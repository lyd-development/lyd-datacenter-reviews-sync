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
from urllib.parse import urlsplit

from apify import Actor
from playwright.async_api import async_playwright

from .browser import attach_proxy_auth, launch_context
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

# Named (cross-run persistent) Key-Value store holding cookies from a
# manually-logged-in Google session — see browser.py's launch_context()
# docstring comment for why this is needed (anonymous sessions get a
# sign-in interstitial that blocks the Sort-by-Newest click specifically).
# Login itself is never done by this Actor — Google rejects automated
# Playwright logins outright — so this store has to be populated manually
# (export a real, human-driven browser's storage_state and upload it under
# the STORAGE_STATE key) and refreshed whenever the session expires.
GOOGLE_SESSION_STORE_NAME = "google-review-session"
GOOGLE_SESSION_STORE_KEY = "STORAGE_STATE"

# Fixed Apify Proxy session id, used ONLY when a stored Google session is
# present — confirmed live (2026-10-19) that a logged-in session loaded
# fine (no error) but Google still silently treated the run as logged-out
# (screenshot showed the "Sign in" button) when the runtime proxy IP
# differed from whatever IP the login cookies were originally issued from.
# The login itself must be performed through this exact same sticky proxy
# session (see the manual-login runbook), so pinning to a fixed id here —
# instead of rotating per place_id/attempt like the anonymous path below —
# is what keeps the runtime IP consistent with the login IP.
GOOGLE_LOGIN_PROXY_SESSION_ID = "google_login_session_v2"

# How many fresh-browser-context attempts to give the setup phase (open
# location -> open Reviews tab -> sort Newest) before giving up. Raised
# 2 -> 3 (2026-09-06) alongside proxy rotation below — a live test run
# (place_id ChIJ84FTuLhG0i0R1rMrarZInMo) showed the sort-click failure isn't
# always the session-throttle case this loop was originally built for: a
# Google "Sign-in to get the best of Google Maps" interstitial can also
# intercept the Sort button's click, and getting past that reliably needs a
# real new session (fresh cookies + a different egress IP), not just one
# more attempt. Each iteration now also requests a new proxy session (see
# proxy_configuration.new_url() below), so a retry isn't just a new
# browser/cookies on the same IP anymore.
MAX_SETUP_ATTEMPTS = 3

# Was a temporary diagnostic True (skip the DOM-scroll fallback so a run
# shows exactly how far RPC-paging alone can get). Now known: RPC-paging
# reliably stops after page 0 with an HTTP 400 (XSRF token mismatch on the
# second call) — a separate, smaller issue from the Sort-click sign-in
# modal this session was mainly chasing. Reverted to False per this flag's
# own original intent, so a real run falls back to scrolling instead of
# stopping after only the first ~10 reviews.
DISABLE_SCROLL_FALLBACK = False

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
        "authorImage": review["author_image"],
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

        # Best-effort — an empty/missing store just means this run falls
        # back to the old anonymous-session behavior (see
        # GOOGLE_SESSION_STORE_NAME above), not a hard failure.
        google_storage_state = None
        try:
            session_store = await Actor.open_key_value_store(name=GOOGLE_SESSION_STORE_NAME)
            google_storage_state = await session_store.get_value(GOOGLE_SESSION_STORE_KEY)
            if google_storage_state:
                Actor.log.info("Loaded a logged-in Google session from the key-value store.")
            else:
                Actor.log.warning(
                    f"No '{GOOGLE_SESSION_STORE_KEY}' value in the '{GOOGLE_SESSION_STORE_NAME}' "
                    "key-value store — continuing with an anonymous (logged-out) session."
                )
        except Exception as session_exc:
            Actor.log.warning(
                f"Could not load the logged-in Google session ({session_exc}) — "
                "continuing with an anonymous (logged-out) session."
            )

        # Off by default (see input_schema.json's useProxy) — this project has
        # run without a proxy since v1 and it's never been required for
        # Google Maps the way it is for TripAdvisor's DataDome protection.
        # When on, rotated per setup attempt below (see MAX_SETUP_ATTEMPTS)
        # so a retry after a failed Newest-sort attempt gets a different
        # egress IP, not just a new browser/cookies on the same one.
        proxy_configuration = None
        if actor_input.get("useProxy", False):
            proxy_input = actor_input.get("proxyConfiguration") or {
                "useApifyProxy": True,
                "apifyProxyGroups": ["RESIDENTIAL"],
            }
            try:
                proxy_configuration = await Actor.create_proxy_configuration(actor_proxy_input=proxy_input)
            except Exception as proxy_exc:
                # e.g. the account/plan doesn't have RESIDENTIAL proxy access —
                # best-effort only, see comment above.
                Actor.log.warning(f"Could not set up a proxy configuration ({proxy_exc}) — continuing without one.")
                proxy_configuration = None
            if proxy_configuration is None:
                Actor.log.warning(
                    "useProxy was on but no proxy configuration is available (Apify proxy not enabled on "
                    "this account/plan) — continuing without a proxy."
                )

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
            browser = context = page = collector = None
            location_info: dict | None = None
            newest_sort_request: dict | None = None
            pre_sort_ids: set[str] = set()

            try:
                # Session/context rotation (2026-08-20) — confirmed live via
                # a local repeated-click repro that the FIRST sort attempt on
                # a brand-new browser context reliably resolves fast, while
                # retrying inside the SAME session (same cookies) after a
                # failure almost never does (Google appears to throttle the
                # Newest-sort RPC per-session once it's been used/failed
                # once). A same-page retry loop inside sort_by_newest can't
                # fix that; a genuinely fresh browser context can, since
                # that's the one condition that's actually worked reliably.
                # Scoped to just the setup phase (open_location through
                # sort_by_newest) — once that succeeds, the rest of the run
                # (RPC paging / scroll collection) has never been the
                # failure point, so it isn't worth restarting for.
                for setup_attempt in range(1, MAX_SETUP_ATTEMPTS + 1):
                    captured_request_holder["value"] = None

                    proxy = None
                    if proxy_configuration is not None:
                        try:
                            # session_id ties this attempt to one sticky proxy IP.
                            # With a stored Google session, it MUST stay fixed
                            # (GOOGLE_LOGIN_PROXY_SESSION_ID) across every attempt so
                            # the runtime IP matches whatever IP the login cookies
                            # came from — see that constant's comment. Without a
                            # stored session there's no IP to match, so it differs
                            # per attempt (place_id + attempt number) instead, so a
                            # retry actually rotates to a new IP.
                            session_id = (
                                GOOGLE_LOGIN_PROXY_SESSION_ID
                                if google_storage_state
                                else f"{place_id}_{setup_attempt}"
                            )
                            proxy_url = await proxy_configuration.new_url(session_id=session_id)
                            parsed_proxy = urlsplit(proxy_url)
                            proxy = {"server": f"http://{parsed_proxy.hostname}:{parsed_proxy.port}"}
                            if parsed_proxy.username:
                                proxy["username"] = parsed_proxy.username
                            if parsed_proxy.password:
                                proxy["password"] = parsed_proxy.password
                        except Exception as proxy_url_exc:
                            Actor.log.warning(
                                f"Could not get a proxy URL for setup attempt {setup_attempt} "
                                f"({proxy_url_exc}) — proceeding without a proxy for this attempt."
                            )

                    # headless=False (2026-10-14) — confirmed live that a
                    # logged-in session ALONE isn't enough: a run with
                    # google_storage_state loaded still hit the exact same
                    # Sort-click sign-in modal under headless=True. A local
                    # repro isolating headed vs. headless (both logged-in)
                    # showed headless is what triggers it, independent of
                    # login — so both are needed together. The container
                    # already wraps this process in `xvfb-run` (see the
                    # Dockerfile/run command), which exists specifically to
                    # give a non-headless browser a virtual display to render
                    # into, so this isn't introducing new infra.
                    browser, context = await launch_context(
                        playwright, headless=False, proxy=proxy, storage_state=google_storage_state
                    )
                    page = await context.new_page()
                    await attach_proxy_auth(context, page, proxy)
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
                        pre_sort_ids = set(collector["reviews"].keys())
                        if sort_by == "newest":
                            # A prior "re-navigate fresh right before
                            # sorting" experiment (bgkey/session snapshot
                            # theory, chasing a single missing newest
                            # review) was reverted here — it was explicitly
                            # unverified, and confirmed live (2026-08-19,
                            # Bokashi Berawa run) to cause a hard failure:
                            # sort_by_newest's qv9Egd response never arrived
                            # within 12s on any of 5 retries after the extra
                            # navigation cycle, failing the whole run
                            # outright — worse than the missing-review issue
                            # it was trying to fix. That was a mid-session
                            # re-navigation though, not a fresh browser
                            # context — consistent with the throttle being
                            # session/cookie-scoped, not page-scoped (a new
                            # page in the same context doesn't reset it).
                            newest_sort_request = await sort_by_newest(page)
                            if newest_sort_request:
                                await Actor.set_value("CAPTURED_REQUEST_NEWEST_SORT", newest_sort_request)
                        else:
                            Actor.log.info(
                                'sortBy="relevant" — skipping the sort step, using Google\'s default order.'
                            )
                        break  # setup succeeded — keep this browser/page for the rest of the run
                    except Exception as setup_exc:
                        if setup_attempt == MAX_SETUP_ATTEMPTS:
                            # Don't close here — let it propagate to the
                            # outer except below, which screenshots the
                            # still-open page before closing (useful
                            # diagnostic; closing first would lose that).
                            raise
                        await browser.close()
                        Actor.log.warning(
                            f"Setup failed on browser session {setup_attempt}/{MAX_SETUP_ATTEMPTS} "
                            f"({setup_exc}) — retrying with a brand-new browser context."
                        )

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
                # browser can still be None here if launch_context() itself
                # failed before ever assigning it (e.g. on the very first
                # setup attempt) — guard so that edge case still reaches
                # Actor.fail() below instead of crashing on None.close().
                if browser is not None:
                    await browser.close()
                await Actor.fail(status_message=str(exc))
