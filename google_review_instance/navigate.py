"""Page navigation: opening a location, extracting its info, opening the
Reviews tab, and sorting by Newest.
"""

from __future__ import annotations

import re
from urllib.parse import quote

from playwright.async_api import Page

from .maps_api import parse_batch_execute_body


async def dismiss_consent_dialog(page: Page) -> None:
    for pattern in (r"reject all", r"accept all"):
        try:
            await page.get_by_role("button", name=re.compile(pattern, re.I)).click(timeout=3000)
            return
        except Exception:
            continue


def extract_google_id_from_url(url: str) -> str | None:
    match = re.search(r"!1s0x[0-9a-f]+:(0x[0-9a-f]+)", url, re.IGNORECASE)
    if not match:
        return None
    try:
        return str(int(match.group(1), 16))
    except ValueError:
        return None


async def open_location(page: Page, place_id: str, max_attempts: int = 3) -> None:
    for attempt in range(1, max_attempts + 1):
        try:
            await page.goto("https://www.google.com", wait_until="domcontentloaded")
            break
        except Exception:
            if attempt == max_attempts:
                raise
            await page.wait_for_timeout(3000)
    await dismiss_consent_dialog(page)

    url = f"https://www.google.com/maps/place/?q=place_id:{quote(place_id, safe='')}&hl=en"
    await page.goto(url, wait_until="domcontentloaded")
    await dismiss_consent_dialog(page)
    await page.wait_for_selector("h1", timeout=15000)


async def extract_location_info(page: Page, place_id: str) -> dict:
    name = (await page.locator("h1").first.inner_text()).strip()

    rating_and_count = await page.evaluate(
        """() => {
            const ratingEl = Array.from(document.querySelectorAll('span[aria-label*="star"]')).find((el) =>
                /^\\d/.test((el.getAttribute("aria-label") || "").trim())
            );
            const rating = ratingEl ? parseFloat(ratingEl.getAttribute("aria-label")) : null;

            const countEl = Array.from(document.querySelectorAll("button, span")).find((el) =>
                /^[\\d.,]+\\s*reviews?$/i.test((el.textContent || "").trim())
            );
            const totalReviews = countEl
                ? parseInt(countEl.textContent.replace(/[^\\d]/g, ""), 10)
                : null;
            return { rating, totalReviews: Number.isNaN(totalReviews) ? null : totalReviews };
        }"""
    )

    location_link = page.url
    google_id = extract_google_id_from_url(location_link)
    reviews_link = (
        f"https://search.google.com/local/reviews?placeid={quote(place_id, safe='')}&q=*&authuser=0&hl=en&gl=US"
    )

    return {
        "place_id": place_id,
        "google_id": google_id,
        "name": name,
        "location_link": location_link,
        "reviews_link": reviews_link,
        "avg_rating": rating_and_count.get("rating"),
        "total_reviews": rating_and_count.get("totalReviews"),
    }


async def open_reviews_tab(page: Page, place_id: str, max_attempts: int = 4) -> None:
    reviews_tab = page.locator('button[aria-label^="Reviews for"]').or_(
        page.get_by_role("tab", name=re.compile("reviews", re.I))
    )

    for attempt in range(1, max_attempts + 1):
        try:
            await reviews_tab.first.click(timeout=8000)
            await page.wait_for_selector("[data-review-id]", timeout=15000)
            return
        except Exception:
            if attempt == max_attempts:
                raise
            url = f"https://www.google.com/maps/place/?q=place_id:{quote(place_id, safe='')}&hl=en"
            await page.goto(url, wait_until="domcontentloaded")
            await dismiss_consent_dialog(page)
            await page.wait_for_selector("h1", timeout=15000)
            await page.wait_for_timeout(1500)


# Confirmed via live Playwright inspection (2026-08-18, real Chrome, La
# Santa Rosa — the hotel/villa-category venue that kept failing): the sort
# trigger button's accessible name is NOT always the static "Sort reviews"
# string. On this venue it was "Most relevant" — the button's aria-label
# tracks whatever sort option is CURRENTLY selected, one of these four
# (verified by clicking it and reading the actual dropdown's menuitemradio
# items — "Most relevant", "Newest", "Highest rating", "Lowest rating", all
# role="menuitemradio", exactly the same structure as the non-hotel UI).
# Earlier guess (a separate "Newest" dropdown next to "All reviews",
# inferred from a screenshot) was wrong — there's no separate hotel-UI code
# path needed at all, just a broader button-name match.
SORT_TRIGGER_NAME_PATTERN = re.compile(
    r"^(sort reviews|most relevant|newest|highest rating|lowest rating)$", re.I
)


async def sort_by_newest(page: Page, max_attempts: int = 5) -> dict | None:
    """Returns the raw request (URL/headers/postData) that produced the
    confirmed newest-sorted response — the caller (main.py) needs this
    specific request as the RPC-pagination template, NOT whatever request the
    collector captured when the Reviews tab first opened (that one reflects
    Google's default "Most relevant" order, from before this function ever
    ran — using it for pagination silently ignores the sort entirely, which
    is exactly what caused a real run's RPC-paged output to come back
    unsorted)."""
    # Confirmed on a real run against a large venue (22568 reviews): a
    # loading overlay (class "mYFZJb") still intercepted pointer events on
    # the Sort button after only 1500ms, causing every click to fail for
    # 3 straight attempts. A slower-proven 4000ms initial wait avoids this —
    # the faster scroll pacing this app uses is about the scroll loop
    # itself, not this one-time setup step, so there's no speed reason to
    # keep it shorter here.
    await page.wait_for_timeout(4000)

    sort_button = page.get_by_role("button", name=SORT_TRIGGER_NAME_PATTERN)
    newest_option = page.get_by_role("menuitemradio", name=re.compile("newest", re.I))

    try:
        has_sort_button = await sort_button.first.is_visible()
    except Exception:
        has_sort_button = False

    if not has_sort_button:
        raise RuntimeError(
            "This venue's Reviews tab has no recognizable sort control "
            "(checked for 'Sort reviews' and the dynamic-label variant "
            "'Most relevant'/'Newest'/'Highest rating'/'Lowest rating'). Not supported yet."
        )

    for attempt in range(1, max_attempts + 1):
        try:
            # Was raw DOM el.click() via page.evaluate() — introduced after a
            # persistent overlay (class "mYFZJb") was confirmed intercepting
            # pointer events on the Sort button on large venues (22568+
            # reviews), and real .click()'s pixel-coordinate hit-testing
            # always lost to it. That raw approach had a hidden cost though:
            # neither el.click() call had an explicit timeout, so a menu that
            # never opened silently ate Playwright's 30s default per attempt
            # — confirmed as a real bug live (2026-08-19, La Plancha, a
            # non-hotel venue): the actor run hung for 30s on
            # newest_option's click before this loop even got to retry,
            # while the identical code opened the menu fine in a local
            # Windows/Chrome test — pointing at container-specific
            # flakiness, not a category-specific DOM difference. Switched to
            # real Playwright clicks with force=True instead: force= skips
            # the same hit-testing that made the overlay a problem in the
            # first place, while still dispatching a genuine trusted click
            # (unlike page.evaluate's synthetic one) — and explicit short
            # timeouts mean a failed click now fails fast so the outer retry
            # loop can actually cycle within a reasonable total time budget.
            async with page.expect_response(
                lambda res: "batchexecute" in res.url and "qv9Egd" in res.url, timeout=12000
            ) as response_info:
                await sort_button.click(force=True, timeout=8000)
                # Confirm the menu actually opened before waiting on a
                # specific item inside it — fails fast here (4s) instead of
                # letting newest_option's own click silently eat another
                # 8-30s waiting on a menu that never appeared this attempt.
                await newest_option.wait_for(state="attached", timeout=4000)
                await newest_option.click(force=True, timeout=8000)
            response = await response_info.value
            body_bytes = await response.body()
            parsed = parse_batch_execute_body(body_bytes.decode("utf-8"))
            timestamps = [r["review_timestamp"] for r in (parsed or {}).get("reviews", [])]
            is_newest_first = len(timestamps) > 0 and all(
                i == 0 or timestamps[i] <= timestamps[i - 1] for i in range(len(timestamps))
            )
            if not is_newest_first:
                raise RuntimeError(
                    "Sort click resolved but the next batch wasn't in newest-first order — "
                    "Google is still serving a different sort."
                )

            request = response.request
            captured_request = {
                "url": request.url,
                "method": request.method,
                "headers": request.headers,
                "postData": request.post_data,
            }

            # Went 800ms -> 5s -> 15s across repeated real runs: the single
            # newest review (confirmed via manual Google Maps screenshot)
            # stayed missing every time, regardless of how long this wait
            # was. That rules out "waiting after the sort click" as the
            # fix — reverted to a small settle delay; main.py now tries a
            # different lever instead (a fresh navigation right before this
            # function runs, not a longer pause here).
            await page.wait_for_timeout(1500)
            return captured_request
        except Exception:
            if attempt == max_attempts:
                raise
            await page.wait_for_timeout(2000)
