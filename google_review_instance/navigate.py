"""Page navigation: opening a location, extracting its info, opening the
Reviews tab, and sorting by Newest.
"""

from __future__ import annotations

import asyncio
import re
from urllib.parse import quote

from apify import Actor
from playwright.async_api import Page

from .maps_api import UnexpectedResponseFormatError, parse_batch_execute_body


async def dismiss_consent_dialog(page: Page) -> None:
    # A real consent dialog renders near-instantly on page load if it's
    # going to show at all — no reason to wait seconds for one. Short
    # timeout here so the common case (no dialog at all, e.g. on Apify's
    # context) doesn't burn up to 6s per call (called twice per
    # open_location) waiting for buttons that were never coming.
    for pattern in (r"reject all", r"accept all"):
        try:
            await page.get_by_role("button", name=re.compile(pattern, re.I)).click(timeout=800)
            return
        except Exception:
            continue
    await dismiss_signin_prompt(page)


async def dismiss_signin_prompt(page: Page) -> None:
    # Separate from dismiss_consent_dialog's reject/accept buttons — this is
    # Google's "Sign-in to get the best of Google Maps" interstitial (a
    # different dialog, confirmed via screenshot 2026-08-20, real Chrome,
    # searching "La Plancha"). It never matches the reject/accept-all
    # patterns, so it was left sitting on top of the page unhandled,
    # blocking whatever click happened next underneath it — including, once
    # confirmed live, causing a force=True click on the Sort button to
    # silently land on the modal's overlay instead (force= only skips
    # Playwright's own actionability check, it doesn't click *through*
    # whatever's actually on top at those coordinates), so
    # batchexecute_requests_seen_this_run came back completely empty — not
    # even the menu-opening RPCs fired.
    # Was scoped to page.get_by_role("dialog").filter(...) — never matched
    # (confirmed live, same incident: the prompt was on screen but this
    # dismiss call was a silent no-op every time). Google's dialog here
    # evidently isn't exposing role="dialog", so drop that requirement
    # entirely and just match the "Dismiss" button by name directly — an
    # exact, case-insensitive "Dismiss" button elsewhere in the Reviews UI
    # is not a realistic false-positive risk.
    try:
        await page.get_by_role("button", name=re.compile(r"^dismiss$", re.I)).first.click(timeout=800)
    except Exception:
        pass


def extract_google_id_from_url(url: str) -> str | None:
    match = re.search(r"!1s0x[0-9a-f]+:(0x[0-9a-f]+)", url, re.IGNORECASE)
    if not match:
        return None
    try:
        return str(int(match.group(1), 16))
    except ValueError:
        return None


def extract_lat_lng_from_url(url: str) -> tuple[float | None, float | None]:
    # Confirmed live (2026-08-23): the canonical "/place/Name/@lat,lng,zoom/
    # data=..." URL also embeds the same coordinates a second time as
    # "!8m2!3d{lat}!4d{lng}" inside the data= block — that second copy is
    # used here since it's present even when the URL is truncated/rewritten
    # in ways that drop the leading "@lat,lng".
    match = re.search(r"!8m2!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)", url)
    if not match:
        return None, None
    try:
        return float(match.group(1)), float(match.group(2))
    except ValueError:
        return None, None


# Best-effort address parsing — confirmed live (2026-08-23) that Google's
# address format varies a lot by country:
#   US: "1037 McKinley Ave, Columbus, OH 43222, United States"
#   Indonesia: "Jl. Double Six, Seminyak, Kec. Kuta, Kabupaten Badung, Bali 80361"
# The Indonesian form has no trailing country segment at all and fuses the
# postal code onto the province name, so there is no single comma-position
# rule that works for both. This extracts what it reliably can (postal code,
# via a bare 4-6 digit token) and falls back to positional heuristics for the
# rest — city/region/country will often be null or approximate for
# non-US-style addresses. Accepted tradeoff (confirmed with the user) rather
# than blocking on a fully general international address parser.
def parse_address_components(address: str) -> dict:
    postal_match = re.search(r"\b(\d{4,6}(?:-\d{4})?)\b(?!.*\d)", address)
    postal_code = postal_match.group(1) if postal_match else None

    segments = [s.strip() for s in address.split(",") if s.strip()]
    if postal_code and segments:
        # Strip the postal code out of whichever segment holds it (it may be
        # fused onto a region name, e.g. "Bali 80361").
        segments = [re.sub(rf"\s*{re.escape(postal_code)}\s*$", "", s).strip() or s for s in segments]

    country = None
    # Only trust a country segment when it's recognizably a country name
    # (not a fused "Province ZIP" segment with the ZIP already stripped) —
    # confirmed live that Indonesian addresses simply omit a country segment
    # entirely, so guessing one from position would be wrong more often than
    # right.
    KNOWN_COUNTRY_NAMES = {"united states", "indonesia", "united kingdom", "australia", "singapore"}
    if segments and segments[-1].strip().lower() in KNOWN_COUNTRY_NAMES:
        country = segments.pop()

    region = segments.pop() if segments else None
    city = segments.pop() if segments else None

    return {"city": city, "region": region, "postal_code": postal_code, "country": country}


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

    # Google Maps rewrites the URL from the plain "?q=place_id:..." query
    # form to the canonical "/place/Name/@lat,lng,zoom/data=!3m1...!1s0x..."
    # form via client-side history.replaceState() shortly after the page
    # itself renders — h1 becoming visible doesn't guarantee this rewrite
    # has happened yet. Confirmed live: capturing page.url immediately after
    # h1 sometimes still had the plain query-string URL, which has neither
    # the "@lat,lng" nor the "!1s0x...:0x..." CID marker that
    # extract_google_id_from_url() and review_link.py's build_review_link()
    # both require — silently producing a null google_id and a null
    # reviewLink on every review for that run. Best-effort wait for the
    # rewrite; proceed with whatever URL is current if it doesn't happen in
    # time rather than blocking indefinitely.
    try:
        await page.wait_for_function(
            "() => /!1s0x[0-9a-f]+:0x[0-9a-f]+/i.test(location.href)", timeout=6000
        )
    except Exception:
        pass

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

            // Confirmed live via Playwright (2026-08-23, real Chrome, "La
            // Plancha" — matches docs/sources/apify reference dataset's
            // known category "Bar"): the category button's jsaction middle
            // token ("wfvdle15") isn't the static "category" string alone,
            // so match on the suffix. This is distinct from the "similar
            // places nearby" carousel's category labels, which render as
            // plain <div class="Q5g20"> with no jsaction attribute at all
            // — confirmed via a full header-HTML dump, exactly one
            // jsaction*="category" match on the page.
            const categoryEl = document.querySelector('button[jsaction*="category"]');
            const category = categoryEl ? categoryEl.textContent.trim() : null;

            // Business info fields, all confirmed live (2026-08-23) via
            // their aria-label prefix — the same pattern Google uses
            // consistently for every info-panel row (see "Address: ...",
            // "Phone: ...", etc. in a real header-HTML dump).
            const byAriaLabelPrefix = (prefix) =>
                Array.from(document.querySelectorAll("button[aria-label], a[aria-label]")).find((el) =>
                    (el.getAttribute("aria-label") || "").toLowerCase().startsWith(prefix)
                );
            const stripPrefix = (el, prefix) =>
                el ? el.getAttribute("aria-label").replace(new RegExp("^" + prefix + "\\\\s*", "i"), "").trim() : null;

            const addressEl = byAriaLabelPrefix("address:");
            const phoneEl = byAriaLabelPrefix("phone:");
            const plusCodeEl = byAriaLabelPrefix("plus code:");
            const websiteEl = Array.from(document.querySelectorAll("a[aria-label]")).find((el) =>
                (el.getAttribute("aria-label") || "").toLowerCase().startsWith("website:")
            );

            // Each day-of-week row has its own aria-label, e.g. "Monday,
            // 6.30 am to 5.00 pm, Copy open hours" / "Saturday, Closed,
            // Copy open hours" — confirmed live, present for every day even
            // when closed.
            const dayNames = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
            const openingHours = dayNames
                .map((day) => {
                    const re = new RegExp("^" + day + ",\\\\s*(.+?),\\\\s*Copy open hours$", "i");
                    const el = Array.from(document.querySelectorAll("[aria-label]")).find((e) =>
                        re.test((e.getAttribute("aria-label") || "").trim())
                    );
                    if (!el) return null;
                    const match = (el.getAttribute("aria-label") || "").match(re);
                    const hoursText = match ? match[1].trim() : null;
                    return { day, hours: hoursText && !/^closed$/i.test(hoursText) ? [hoursText] : [] };
                })
                .filter(Boolean);

            // Open/closed status text, e.g. "Closed · Opens 6.30 am Mon".
            // Confirmed live (2026-08-23): there is no aria-label on this
            // element at all (an earlier attempt assumed one and always
            // returned null) — it's a plain <span> whose direct children
            // are two styled text spans ("Closed"/"Open" + " · Opens/Closes
            // ..."). No stable class name to key off either (generated,
            // e.g. "ZDu9vd"), so instead scan all spans for one whose
            // trimmed textContent matches the status+detail pattern and
            // take the shortest match — the innermost element containing
            // exactly this text, not a larger ancestor that also contains
            // it.
            const statusMatches = Array.from(document.querySelectorAll("span"))
                .map((el) => (el.textContent || "").trim())
                .filter((t) => /(open|closed)\\b.{0,40}(opens|closes|24 hours)/i.test(t) && t.length < 80);
            statusMatches.sort((a, b) => a.length - b.length);
            const openStatus = statusMatches[0] || null;

            const accessibilityAttributes = Array.from(
                document.querySelectorAll('[aria-label][role="img"][data-tooltip]')
            )
                .map((el) => el.getAttribute("aria-label"))
                .filter((label, idx, arr) => label && arr.indexOf(label) === idx);

            return {
                rating,
                totalReviews: Number.isNaN(totalReviews) ? null : totalReviews,
                category: category || null,
                address: stripPrefix(addressEl, "address:"),
                phone: stripPrefix(phoneEl, "phone:"),
                website: websiteEl ? websiteEl.getAttribute("aria-label").replace(/^website:\\s*/i, "").trim() : null,
                plusCode: stripPrefix(plusCodeEl, "plus code:"),
                openingHours,
                openStatus,
                accessibilityAttributes,
            };
        }"""
    )

    location_link = page.url
    google_id = extract_google_id_from_url(location_link)
    latitude, longitude = extract_lat_lng_from_url(location_link)
    reviews_link = (
        f"https://search.google.com/local/reviews?placeid={quote(place_id, safe='')}&q=*&authuser=0&hl=en&gl=US"
    )

    address = rating_and_count.get("address")
    address_components = parse_address_components(address) if address else {
        "city": None, "region": None, "postal_code": None, "country": None
    }

    category = rating_and_count.get("category")

    return {
        "place_id": place_id,
        "google_id": google_id,
        "name": name,
        "location_link": location_link,
        "reviews_link": reviews_link,
        "avg_rating": rating_and_count.get("rating"),
        "total_reviews": rating_and_count.get("totalReviews"),
        "category": category,
        "categories": [category] if category else None,
        "address": address,
        "city": address_components["city"],
        "region": address_components["region"],
        "postal_code": address_components["postal_code"],
        "country": address_components["country"],
        "latitude": latitude,
        "longitude": longitude,
        "plus_code": rating_and_count.get("plusCode"),
        "phone": rating_and_count.get("phone"),
        "website": rating_and_count.get("website"),
        "opening_hours": rating_and_count.get("openingHours") or None,
        "open_status": rating_and_count.get("openStatus"),
        "accessibility_attributes": rating_and_count.get("accessibilityAttributes") or None,
    }


async def open_reviews_tab(page: Page, place_id: str, max_attempts: int = 2) -> None:
    # Reduced from 4 (2026-08-19) — a venue whose Reviews tab genuinely
    # never loads was burning up to ~158s of billable compute across 4 full
    # attempts before finally giving up. A structural failure here doesn't
    # get more likely to succeed on attempt #4 than #2, and the cron already
    # retries a failed location on its next scheduled tick regardless — so
    # fewer attempts means cheaper failures without giving up on the venue
    # entirely.
    reviews_tab = page.locator('button[aria-label^="Reviews for"]').or_(
        page.get_by_role("tab", name=re.compile("reviews", re.I))
    )

    for attempt in range(1, max_attempts + 1):
        try:
            await dismiss_signin_prompt(page)
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


async def sort_by_newest(page: Page, max_attempts: int = 1) -> dict | None:
    """Returns the raw request (URL/headers/postData) that produced the
    confirmed newest-sorted response — the caller (main.py) needs this
    specific request as the RPC-pagination template, NOT whatever request the
    collector captured when the Reviews tab first opened (that one reflects
    Google's default "Most relevant" order, from before this function ever
    ran — using it for pagination silently ignores the sort entirely, which
    is exactly what caused a real run's RPC-paged output to come back
    unsorted)."""
    # max_attempts reduced 5 -> 3 (2026-08-19) -> 1 (2026-08-20). The 1
    # is not a cost-tuning knob like the earlier reductions — it's because
    # retrying was proven actively useless for this call. Confirmed live via
    # a local repeated-click repro (2026-08-20, La Brisa Bali, same browser
    # context, alternating Most relevant/Newest): the FIRST sort click after
    # a fresh page load resolved in 0.5s every time, but every subsequent
    # click on that same session timed out completely (20s, 0 responses) —
    # this specific RPC appears to be throttled per-session by Google after
    # one use, not slow. A same-page retry (or even a full page
    # re-navigation within the same browser context — already tried and
    # reverted 2026-08-19 after it made every retry fail too, consistent
    # with a session/cookie-level throttle rather than a page-level one)
    # cannot out-wait or dodge that. The only thing that's ever actually
    # resolved fast is a brand-new browser context — which is exactly what
    # the next cron-scheduled Actor run gets. So one attempt, fail fast, let
    # the next real run (fresh session) be the retry.
    # Confirmed on a real run against a large venue (22568 reviews): a
    # loading overlay (class "mYFZJb") still intercepted pointer events on
    # the Sort button after only 1500ms, causing every click to fail for
    # 3 straight attempts. Raised 4000->6000ms (2026-08-20) — confirmed live
    # (La Plancha) that the sort click can resolve with a real 200 response
    # containing zero reviews (not a wrong-order response, an EMPTY one),
    # right after open_reviews_tab returned. open_reviews_tab only waits for
    # the first [data-review-id] to attach, not for Google's own initial
    # review batch to finish loading — a screenshot at that moment showed
    # the review list still mid-spinner. A bit more settle time here reduces
    # the odds of racing that. The faster scroll pacing this app uses is
    # about the scroll loop itself, not this one-time setup step, so there's
    # no speed reason to keep it shorter.
    await page.wait_for_timeout(8000)

    sort_button = page.get_by_role("button", name=SORT_TRIGGER_NAME_PATTERN)
    newest_option = page.get_by_role("menuitemradio", name=re.compile("newest", re.I))
    most_relevant_option = page.get_by_role("menuitemradio", name=re.compile("most relevant", re.I))

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

    # Diagnostic only (2026-08-20) — three different click strategies (raw
    # evaluate click, force=True real click, DOM el.click()) have all
    # "succeeded" with no exception while the matching batchexecute response
    # never arrives, even at 25000ms. That rules out both "too slow" and
    # "click missed its target". Logging every batchexecute request seen
    # (not just ones matching our rpcid filter) tells us whether the click
    # is triggering ANY request at all, or a request with an unexpected
    # rpcid/shape our filter doesn't match.
    seen_batchexecute_requests: list[str] = []
    failed_batchexecute_requests: list[dict] = []

    def _record_request(request):
        if "batchexecute" in request.url:
            seen_batchexecute_requests.append(request.url)

    def _record_request_failed(request):
        # Confirmed live (2026-08-20, La Brisa Bali): a batchexecute request
        # for the Newest-sort RPC does fire (seen_batchexecute_requests
        # proves the click works), but no matching "response" event ever
        # arrives even at 25000ms — expect_response resolves on ANY
        # response including error statuses, so a true timeout here means
        # the request never completed at all, not that it was slow or
        # errored normally. This listener catches Playwright's explicit
        # network-level failure reason (e.g. connection reset/aborted) for
        # that exact request, which "response" alone can't tell us.
        if "batchexecute" in request.url:
            failed_batchexecute_requests.append({"url": request.url, "failure": request.failure})

    page.on("request", _record_request)
    page.on("requestfailed", _record_request_failed)

    try:
        for attempt in range(1, max_attempts + 1):
            try:
                # History: raw DOM el.click() via page.evaluate() -> real
                # Playwright click(force=True) -> raised the response timeout
                # 12000->25000, none of it fixed the actual symptom (confirmed
                # live, 2026-08-20: even 25000ms still timed out on every
                # attempt against La Brisa Bali). Switching to a DOM-level
                # el.click() on the live element (ruling out coordinate
                # drift) *also* didn't fix it — same timeout, same symptom.
                # Three different click strategies all "succeed" with no
                # response ever arriving means this isn't about how we
                # click. seen_batchexecute_requests below (logged for every
                # request matching "batchexecute", not just our rpcid
                # filter) is there to answer the actual open question: does
                # the click trigger any request at all?
                async def _click_and_capture(option):
                    async with page.expect_response(
                        lambda res: "batchexecute" in res.url and "qv9Egd" in res.url, timeout=15000
                    ) as response_info:
                        await sort_button.click(force=True, timeout=8000)
                        # Confirm the menu actually opened before waiting on
                        # a specific item inside it — fails fast here (4s)
                        # instead of letting option's own click silently eat
                        # another long wait on a menu that never appeared
                        # this attempt.
                        await option.wait_for(state="attached", timeout=4000)
                        await asyncio.wait_for(option.evaluate("(el) => el.click()"), timeout=8)
                        # Extra settle time after the click itself
                        # (2026-08-20) — still inside the listener's window,
                        # so this doesn't change whether we catch the
                        # click's response, only gives Google's backend a
                        # bit more time since the click fired before we ask
                        # for it.
                        await page.wait_for_timeout(2000)
                    resp = await response_info.value
                    body_bytes = await resp.body()
                    parsed = parse_batch_execute_body(body_bytes.decode("utf-8"))
                    return [r["review_timestamp"] for r in (parsed or {}).get("reviews", [])], resp

                await dismiss_signin_prompt(page)
                pre_click_label = await sort_button.first.inner_text()
                timestamps, response = await _click_and_capture(newest_option)

                if not timestamps:
                    # Confirmed live 2026-08-20 (La Plancha, then again on La
                    # Favela even after generous settle waits): the sort
                    # click can succeed with a fast, real 200 response that
                    # still contains zero reviews — looks like Google's
                    # "Newest" cache for this venue is cold on the first hit
                    # of a session, not a loading-race issue extra delay can
                    # fix (already tried, didn't help on La Favela). Directly
                    # re-clicking Newest again wouldn't refire the RPC —
                    # confirmed earlier that re-selecting an
                    # already-selected option is a no-op (no state change,
                    # no new request). So force one first: toggle to "Most
                    # relevant" (a real selection change) then back to
                    # "Newest" — the exact same click sequence already
                    # proven to reliably fire a fresh qv9Egd request.
                    # Best-effort — if the toggle itself fails for any
                    # reason, still fall through and try the real
                    # newest-click once more anyway.
                    try:
                        await dismiss_signin_prompt(page)
                        await sort_button.click(force=True, timeout=8000)
                        await most_relevant_option.wait_for(state="attached", timeout=4000)
                        await asyncio.wait_for(most_relevant_option.evaluate("(el) => el.click()"), timeout=8)
                        await page.wait_for_timeout(1500)
                    except Exception as toggle_exc:
                        Actor.log.warning(f"Sort-toggle retry step failed, trying anyway: {toggle_exc}")
                    timestamps, response = await _click_and_capture(newest_option)

                if not timestamps:
                    raise RuntimeError(
                        "Sort click resolved but the response contained zero reviews (even after "
                        "a toggle-and-retry) — Google's Newest cache for this venue may be cold."
                    )

                # Requiring a STRICT non-increasing order was too fragile — confirmed
                # live (La Brisa Bali, 2026-08-18/20): a real Newest-sorted response
                # had a review out of strict chronological order (an edit or an
                # owner-reply bump moves a review's feed position without changing
                # the timestamp we read), which made this check reject a genuinely
                # correct response every time. Tolerating a small fraction of
                # adjacent inversions still catches the real failure mode this
                # guards against (Google silently still serving "Most relevant"
                # order, which is scrambled throughout, not off-by-one).
                inversions = sum(
                    1 for i in range(1, len(timestamps)) if timestamps[i] > timestamps[i - 1]
                )
                inversion_rate = inversions / (len(timestamps) - 1) if len(timestamps) > 1 else 0
                if inversion_rate > 0.2:
                    # Numbers included directly in the message (not just a
                    # separate diagnostic field) so they show up in the
                    # Actor's run log / traceback without needing extra
                    # instrumentation.
                    raise RuntimeError(
                        "Sort click resolved but the next batch wasn't in newest-first order — "
                        "Google is still serving a different sort. "
                        f"(inversion_rate={inversion_rate:.2f}, reviews={len(timestamps)}, "
                        f"timestamps={[t.isoformat() for t in timestamps]})"
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
            except Exception as exc:
                # Loud and specific for a format-drift error (see maps_api.py's
                # KNOWN_FRAME_IDENTIFIERS) — this is a "go fix the parser"
                # situation, not a flaky-interaction one, so it shouldn't get
                # buried among the usual timeout/empty-response noise below.
                if isinstance(exc, UnexpectedResponseFormatError):
                    Actor.log.error(f"sort_by_newest: {exc}")

                # Diagnostic capture — this exact interaction fails reliably on
                # some venues in Apify's container while being unreproducible
                # locally. Best-effort only — a capture failure must never mask
                # the real error below.
                try:
                    await page.screenshot(path="/tmp/sort_click_failure.png")
                    await Actor.set_value(
                        "SORT_CLICK_FAILURE_SCREENSHOT",
                        open("/tmp/sort_click_failure.png", "rb").read(),
                        content_type="image/png",
                    )
                    menu_html = await page.evaluate(
                        """() => {
                            const items = Array.from(document.querySelectorAll('[role="menuitemradio"]'));
                            return items.map((el) => el.outerHTML).join("\\n---\\n") || "(no menuitemradio elements found)";
                        }"""
                    )
                    try:
                        post_click_label = await sort_button.first.inner_text()
                    except Exception:
                        post_click_label = None
                    await Actor.set_value(
                        "SORT_CLICK_FAILURE_STATE",
                        {
                            "attempt": attempt,
                            "error": str(exc),
                            "menu_html": menu_html,
                            "sort_button_label_before_click": pre_click_label,
                            "sort_button_label_after_click": post_click_label,
                            "batchexecute_requests_seen_this_run": list(seen_batchexecute_requests),
                            "batchexecute_requests_failed_this_run": list(failed_batchexecute_requests),
                        },
                    )
                except Exception as capture_exc:
                    Actor.log.warning(f"Diagnostic capture itself failed: {capture_exc}")

                if attempt == max_attempts:
                    raise
                await page.wait_for_timeout(2000)
    finally:
        page.remove_listener("request", _record_request)
        page.remove_listener("requestfailed", _record_request_failed)
