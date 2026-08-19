"""Entry point for this Actor. A request-capture test run (see
scroll_reviews.py's on_request_captured) confirmed the underlying Playwright
network-interception technique works reliably with fast scroll pacing (442
reviews in ~3 minutes on a real run). Output uses a full, stable record
shape — same dataset columns the rest of this project's dashboard expects —
so a run here can go straight through the dashboard's "Import CSV" button.

**Open risk, not yet resolved**: the fast pacing here is unverified at
larger scale or repeated runs. A slower, more cautious pacing was previously
used elsewhere as a deliberate response to a real, confirmed bug (a
freshly-posted review going missing from an automated session's Newest-sort
response). Whether faster pacing here reintroduces that risk is untested;
treat this app's results with that caveat until proven otherwise on more
venues/runs.

Also captures the raw batchexecute request on first sight (see
scroll_reviews.py) and, since that capture confirmed the request carries a
`pagination_token` Google's own response already provides (see
rpc_paginate.py), this run now tries paging through that RPC directly
instead of scrolling the DOM — the same technique the Apify Store's
compass/google-maps-reviews-scraper actor appears to use, and the likely
reason its per-page timing doesn't degrade with depth the way our DOM-scroll
approach does. This is genuinely unverified (does Google accept a reused
bgkey/bgbind pair across a paged sequence of calls?) — the first failed call
gives up on it for the rest of the run and falls back to the proven DOM
scroll approach, so a wrong guess here degrades gracefully instead of
failing the whole run.
"""

from __future__ import annotations

import asyncio
import json as json_module
import random
import re
from datetime import datetime

from apify import Actor
from playwright.async_api import async_playwright

from .browser import launch_context
from .navigate import extract_location_info, open_location, open_reviews_tab, sort_by_newest
from .review_link import build_review_link
from .rpc_paginate import fetch_next_page
from .scroll_reviews import _sort_newest_first, create_review_collector, scroll_and_collect_reviews

# Tried page_size=100, then 50, on real runs: both rejected immediately
# (page 0 failed instantly, every time) — not just large deviations, ANY
# change from the template's own value (10, whatever Google's real client
# actually requested) gets rejected. Settled: page-size can't be increased.
# Kept at None (use the template's own value) — do not re-attempt a
# different number without new evidence.
RPC_PAGE_SIZE = None

# Temporary diagnostic flag: skip the DOM-scroll fallback entirely so a run
# shows exactly how far RPC-paging alone can get (and why it stops) without
# scrolling masking the true stop point. Set back to False once that's known
# — a real backfill run should still fall back to scrolling on failure.
DISABLE_SCROLL_FALLBACK = True

# A deliberately fast pacing — confirmed working on one real run (442
# reviews, no errors, no missing-review symptoms observed) but not yet
# proven safe at scale. See module docstring.
SCROLL_DELAY_MIN_MS = 1_000
SCROLL_DELAY_MAX_MS = 2_000
MAX_SCROLL_ATTEMPTS_WITHOUT_NEW_REVIEWS = 20


def _iso(value: object) -> object:
    return value.isoformat() if isinstance(value, datetime) else value


# Stable dataset column shape the dashboard's CSV import expects.
def to_dataset_record(review: dict) -> dict:
    return {
        "reviewId": review["review_id"],
        "authorName": review["author_name"],
        "authorId": review["author_id"],
        "authorReviewsCount": review["author_reviews_count"],
        "authorPhotosCount": review["author_photos_count"],
        "authorIsLocalGuide": review["author_is_local_guide"],
        "reviewRating": review["review_rating"],
        "reviewTimestamp": _iso(review["review_timestamp"]),
        "sourceReview": review["review_source"],
        "reviewTextOriginal": review["review_text_original"],
        "reviewTextTranslated": review["review_text_translated"],
        "reviewLanguage": review["review_language"],
        "ownerAnswer": review["owner_answer"],
        "ownerAnswerTimestamp": _iso(review["owner_answer_timestamp"]),
        "reviewLink": review["review_link"],
        "reviewImgUrl": review["review_img_url"],
        "reviewImgUrls": json_module.dumps(review["review_img_urls"]) if review["review_img_urls"] else None,
        "authorLink": review["author_link"],
        "subRatings": json_module.dumps(review["sub_ratings"]) if review["sub_ratings"] else None,
        "reviewAttributes": json_module.dumps(review["review_attributes"]) if review["review_attributes"] else None,
    }


async def main() -> None:
    async with Actor:
        actor_input = await Actor.get_input() or {}
        place_id = actor_input.get("placeId")
        max_reviews = actor_input.get("maxReviews") or None
        sort_by = actor_input.get("sortBy", "newest")

        if not place_id:
            Actor.log.error("Input must include `placeId` — the Google Maps place_id of the venue to scrape.")
            await Actor.fail(status_message="Missing required input: placeId")
            return

        captured_request_holder: dict = {"value": None}

        async def on_request_captured(request_info: dict) -> None:
            captured_request_holder["value"] = request_info
            await Actor.set_value("CAPTURED_REQUEST", request_info)
            Actor.log.info(
                "Captured the raw batchexecute request (URL/headers/POST body) — "
                "saved to the CAPTURED_REQUEST key in this run's Key-Value store."
            )

        async def on_aborting() -> None:
            await asyncio.sleep(1)
            await Actor.exit()

        Actor.on("aborting", on_aborting)

        pushed_count = 0

        async with async_playwright() as playwright:
            browser, context = await launch_context(playwright, headless=True)
            page = await context.new_page()
            collector = create_review_collector(page, on_request_captured=on_request_captured)

            try:
                Actor.log.info(f"Opening location for place_id={place_id}...")
                await open_location(page, place_id)
                location_info = await extract_location_info(page, place_id)
                Actor.log.info(
                    f"Location: {location_info['name']} — "
                    f"{location_info.get('total_reviews', '?')} reviews reported by Google"
                )
                await Actor.set_value("LOCATION_INFO", location_info)

                await open_reviews_tab(page, place_id)
                pre_sort_ids: set[str] = set(collector["reviews"].keys())
                newest_sort_request: dict | None = None
                if sort_by == "newest":
                    # A prior "re-navigate fresh right before sorting"
                    # experiment (bgkey/session snapshot theory, chasing a
                    # single missing newest review) was reverted here — it
                    # was explicitly unverified, and confirmed live
                    # (2026-08-19, Bokashi Berawa run) to cause a hard
                    # failure: sort_by_newest's qv9Egd response never arrived
                    # within 12s on any of 5 retries after the extra
                    # navigation cycle, failing the whole run outright —
                    # worse than the missing-review issue it was trying to
                    # fix. Back to a single navigation (open_reviews_tab
                    # above, already run once) — a proven-stable pattern.
                    newest_sort_request = await sort_by_newest(page)
                    if newest_sort_request:
                        await Actor.set_value("CAPTURED_REQUEST_NEWEST_SORT", newest_sort_request)
                else:
                    Actor.log.info('sortBy="relevant" — skipping the sort step, using Google\'s default order.')

                # The RPC-pagination template must reflect whichever sort is
                # actually active. Using the tab-open request (captured
                # before any sort click, always "Most relevant") for a
                # sortBy="newest" run silently ignores the sort entirely —
                # confirmed as a real bug on a live run (RPC-paged output
                # came back unsorted despite sort_by_newest() having
                # succeeded). newest_sort_request is the request that
                # produced the confirmed-newest-sorted response instead.
                pagination_request = newest_sort_request if sort_by == "newest" else captured_request_holder["value"]

                def on_progress(count: int, new_count: int) -> None:
                    Actor.log.debug(f"Collected so far: {count} ({new_count} new)")

                async def on_flush(new_reviews: list[dict]) -> None:
                    nonlocal pushed_count
                    await Actor.push_data([to_dataset_record(r) for r in new_reviews])
                    pushed_count += len(new_reviews)
                    Actor.log.info(f"Pushed {len(new_reviews)} reviews to the dataset ({pushed_count} total so far).")

                # Push whatever's already in collector["reviews"] BEFORE
                # RPC-paging starts (the pre-sort batch + the sort's own
                # first confirmed response — these are the actual newest
                # reviews on a sortBy="newest" run). Real bug found on a run:
                # pushing this batch only at the very end (after 190+
                # RPC-paged reviews) put Apify's Dataset itself in the wrong
                # order — Dataset/CSV export order is push order, not
                # auto-sorted — so the genuinely newest reviews (confirmed
                # present via DEAN SIA showing up in the export) ended up at
                # the BOTTOM of the file instead of the top, even though the
                # data itself was no longer missing.
                already_flushed_ids: set[str] = set()
                initial_batch = list(collector["reviews"].values())
                if initial_batch:
                    # review_link is otherwise only built inside the RPC loop
                    # below (per fetched page) — this batch arrived via the
                    # page-level response listener instead, so it never went
                    # through that step. Left unbuilt, every record here
                    # would ship with reviewLink: null.
                    for r in initial_batch:
                        r["review_link"] = build_review_link(location_info["location_link"], r["review_id"])
                    await on_flush(_sort_newest_first(initial_batch) if sort_by == "newest" else initial_batch)
                    already_flushed_ids.update(r["review_id"] for r in initial_batch)

                # Try paging through the RPC directly (no DOM scroll) before
                # falling back to the proven scroll approach — see module
                # docstring. Only attempted if the request-capture step above
                # actually got something to work from, and only while it
                # keeps succeeding; the first failed/empty call stops this
                # and hands off to scroll_and_collect_reviews below with
                # whatever was already collected (and already pushed) so far.
                exclude_from_cap = pre_sort_ids if sort_by == "newest" else set()
                rpc_pages_done = 0
                stop_reason: str | None = "no pagination_token available after sort (nothing to page through)"
                if pagination_request:
                    reqid_match = re.search(r"_reqid=(\d+)", pagination_request["url"])
                    reqid = int(reqid_match.group(1)) if reqid_match else 100000
                    token = collector["state"]["pagination_token"]
                    # Diagnostic (2026-08-18): a run against a hotel-category
                    # venue got token=None/empty immediately, skipping RPC-
                    # paging entirely with "reason: None" (stop_reason's
                    # unset initial value, never actually set since the while
                    # loop body never ran) — no explanation logged for WHY
                    # the token was empty. Logging the raw value + how many
                    # reviews the collector already has here to actually see
                    # what's happening on the next run instead of guessing.
                    Actor.log.info(
                        f"pagination_token after sort: {token!r} "
                        f"(collector has {len(collector['reviews'])} reviews so far)"
                    )
                    while token:
                        new_count = sum(1 for rid in collector["reviews"] if rid not in exclude_from_cap)
                        if max_reviews and new_count >= max_reviews:
                            stop_reason = "reached maxReviews"
                            break
                        reqid += 1
                        # Confirmed on a real run: ~1576 back-to-back calls
                        # with essentially zero delay between them (as low as
                        # ~180ms) eventually got rejected — a small random
                        # pace between calls is cheap to try and makes the
                        # traffic pattern look less like a burst, before
                        # resorting to giving up on RPC-paging entirely.
                        await asyncio.sleep(random.uniform(0.3, 0.8))
                        parsed, error = await fetch_next_page(
                            page, pagination_request, token, reqid, page_size=RPC_PAGE_SIZE
                        )
                        if not parsed or not parsed["reviews"]:
                            stop_reason = error or "page came back with zero reviews (likely genuinely reached the end)"
                            break
                        for r in parsed["reviews"]:
                            r["review_link"] = build_review_link(location_info["location_link"], r["review_id"])
                            collector["reviews"][r["review_id"]] = r
                        await on_flush(parsed["reviews"])
                        already_flushed_ids.update(r["review_id"] for r in parsed["reviews"])
                        rpc_pages_done += 1
                        Actor.log.info(
                            f"RPC page {rpc_pages_done}: +{len(parsed['reviews'])} reviews without scrolling "
                            f"({len(collector['reviews'])} total so far)."
                        )
                        new_token = parsed.get("pagination_token")
                        if not new_token or new_token == token:
                            stop_reason = "no further pagination_token returned (reached the end)"
                            break
                        token = new_token

                    Actor.log.info(
                        f"RPC-paging stopped after {rpc_pages_done} page(s) — reason: {stop_reason}."
                        + (" Falling back to DOM scroll for the rest of this run."
                           if not DISABLE_SCROLL_FALLBACK
                           else " Scroll fallback is disabled (diagnostic run) — stopping here.")
                    )
                else:
                    Actor.log.info(
                        "No usable request template for RPC-paging (capture step didn't complete in time)"
                        + (" — using DOM scroll for the whole run." if not DISABLE_SCROLL_FALLBACK else ".")
                    )

                if DISABLE_SCROLL_FALLBACK:
                    # Safety net only — the initial batch is now flushed
                    # BEFORE the RPC loop starts (see above) and every RPC
                    # page flushes itself, so this should normally be empty.
                    # Kept in case some other code path adds to
                    # collector["reviews"] without going through on_flush.
                    leftover = [r for rid, r in collector["reviews"].items() if rid not in already_flushed_ids]
                    if leftover:
                        await on_flush(_sort_newest_first(leftover) if sort_by == "newest" else leftover)

                    final_reviews = list(collector["reviews"].values())
                    result = {
                        "reviews": _sort_newest_first(final_reviews) if sort_by == "newest" else final_reviews,
                        "reached_cap": stop_reason == "reached maxReviews",
                    }
                else:
                    result = await scroll_and_collect_reviews(
                        page,
                        collector,
                        max_scroll_attempts_without_new_reviews=MAX_SCROLL_ATTEMPTS_WITHOUT_NEW_REVIEWS,
                        scroll_delay_min_ms=SCROLL_DELAY_MIN_MS,
                        scroll_delay_max_ms=SCROLL_DELAY_MAX_MS,
                        max_reviews=max_reviews,
                        location_url=location_info["location_link"],
                        flush_interval_ms=30_000,
                        flush_batch_size=100,
                        on_progress=on_progress,
                        on_flush=on_flush,
                        exclude_from_cap=exclude_from_cap,
                        sort_output=sort_by == "newest",
                        already_flushed_ids=already_flushed_ids,
                    )

                reached_cap_note = (
                    " (hit the maxReviews cap — set it to 0 for a truly unlimited run.)"
                    if result["reached_cap"]
                    else ""
                )
                Actor.log.info(
                    f"Done. Collected {len(result['reviews'])} reviews, "
                    f"pushed {pushed_count} to the dataset.{reached_cap_note}"
                )

                await browser.close()

            except Exception as exc:
                Actor.log.exception(f"Actor run failed: {exc}")
                try:
                    screenshot = await page.screenshot(full_page=False)
                    await Actor.set_value("ERROR_SCREENSHOT", screenshot, content_type="image/png")
                except Exception:
                    pass
                await browser.close()
                await Actor.fail(status_message=str(exc))
