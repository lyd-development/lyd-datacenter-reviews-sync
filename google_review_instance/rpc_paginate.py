"""Experimental: page through the batchexecute/qv9Egd RPC directly using the
`pagination_token` Google's own response already includes (see
maps_api.py's parse_batch_execute_body), instead of scrolling the DOM to
trigger the next batch. This is the likely reason the Apify Store's
compass/google-maps-reviews-scraper actor's timing doesn't degrade with
depth (see docs/sources/log-build-apify) — they're not scrolling anything,
they're calling this RPC directly with an explicit cursor.

Still uses the real browser session (Playwright's `page.request` shares the
live page's cookies) — this is NOT the earlier no-browser idea. Every call
reuses the anti-bot headers (`x-maps-bgkey`, `x-maps-bgbind`) captured from
the browser's own first real request this session, unmodified except the
pagination cursor slot in the POST body. Whether Google accepts a *reused*
bgkey/bgbind pair for a request with a different cursor is genuinely
unverified — has to be observed on a real run, not assumed. If a call fails
or comes back empty, the caller (main.py) gives up on this approach for the
rest of the run and falls back to normal DOM scrolling — a wrong guess about
the request schema should degrade gracefully, not hard-fail the whole run.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import secrets
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlsplit

from playwright.async_api import Page

from .maps_api import UnexpectedResponseFormatError, parse_batch_execute_body

CID_PAIR_PATTERN = re.compile(r"!1s(0x[0-9a-f]+:0x[0-9a-f]+)", re.IGNORECASE)

# Confirmed live (2026-10-19) that RPC-paging's HTTP 400 ("xsrf" error,
# {"48448350": ["xsrf", "<token>", [...]]}) only started happening once the
# browser carries a logged-in Google session (see browser.py's storage_state
# comment) — anonymous sessions never hit this. The fix isn't a header (every
# header a real browser automatically attaches was tested and made no
# difference); it's this page-embedded anti-CSRF token, which has to ride
# along as a POST body field named "at" on every batchexecute call once
# logged in. Confirmed missing from the captured request template because
# that template is captured via Playwright's own request listener, which
# never sees form fields the page's own JS appends at submit time — "at" is
# one of those, not a network-level header.
SNLM0E_PATTERN = re.compile(r'"SNlM0e":"([^"]+)"')


def extract_cid_pair(location_url: str) -> str | None:
    match = CID_PAIR_PATTERN.search(location_url)
    return match.group(1) if match else None


async def get_xsrf_token(page: Page) -> str | None:
    """Best-effort — returns None on an anonymous session (no WIZ_global_data
    SNlM0e present at all) or if Google's bootstrap data shape changes.
    Callers must tolerate None (just omit "at" and let the call fail/retry
    as it already did before this fix, rather than crashing on a None).

    Reads straight from the live `window.WIZ_global_data` JS global instead
    of serializing+regexing the whole page HTML via page.content() — this is
    also literally how Google's own client reads it, and avoids adding the
    latency of a full-page HTML dump right before a call that appears to be
    time-sensitive (see main.py's caller comment)."""
    try:
        token = await page.evaluate(
            "() => (window.WIZ_global_data && window.WIZ_global_data.SNlM0e) || null"
        )
        if token:
            return token
    except Exception:
        pass
    try:
        html = await page.content()
    except Exception:
        return None
    match = SNLM0E_PATTERN.search(html)
    return match.group(1) if match else None


def _random_correlation_id() -> str:
    """Confirmed live (2026-10-19), comparing a real second-page browser
    request against ours: args[4][0] (a ~22-char base64url-looking string,
    e.g. "Ra7Vau_nCsvt4-EP4-KyyAc") is DIFFERENT on every real request —
    Google's own client generates a fresh one per call, it isn't tied to the
    page/session. Reusing the one captured in the template (same value on
    every one of our calls) is what made RPC-paging come back with a
    well-formed but empty response (`[null,null,null,null,null,true]`, 200
    OK, no error) instead of the next page — no error to catch, just quietly
    the wrong page. No indication Google validates this beyond "looks like a
    fresh random id", so a locally-generated one works as well as extracting
    it from anywhere."""
    return base64.urlsafe_b64encode(secrets.token_bytes(16)).rstrip(b"=").decode("ascii")


def _set_pagination_params(args: list[Any], token: str, page_size: int | None) -> list[Any]:
    """args[1] is [pageSize, paginationToken] in every captured request —
    the cursor always changes between pages; page_size is normally left as
    whatever the captured template already had (10, in every real request
    seen so far — that's Google's own client's choice, not ours) unless a
    caller explicitly overrides it to test whether a bigger page is honored.
    args[4][0] is the per-call correlation id — see _random_correlation_id.
    Everything else (sort context, CID, client date info) is reused verbatim
    from the captured template. Unverified assumption — see module
    docstring."""
    args = json.loads(json.dumps(args))  # deep copy, don't mutate the template
    if isinstance(args, list) and len(args) > 1 and isinstance(args[1], list) and len(args[1]) >= 2:
        size = page_size if page_size is not None else args[1][0]
        args[1] = [size, token]
    if (
        isinstance(args, list)
        and len(args) > 4
        and isinstance(args[4], list)
        and len(args[4]) >= 1
        and isinstance(args[4][0], str)
    ):
        args[4][0] = _random_correlation_id()
    return args


def build_post_data(
    template_post_data: str, pagination_token: str, page_size: int | None = None, xsrf_token: str | None = None
) -> str | None:
    try:
        parsed_qs = parse_qs(template_post_data)
        f_req = json.loads(parsed_qs["f.req"][0])
        inner_args = json.loads(f_req[0][0][1])
        f_req[0][0][1] = json.dumps(_set_pagination_params(inner_args, pagination_token, page_size))
        fields = {"f.req": json.dumps(f_req)}
        # "at" — the page's own anti-CSRF token (see get_xsrf_token above).
        # Only required once the session is logged in; an anonymous session
        # has no SNlM0e to find, so xsrf_token is None and this is omitted
        # exactly like before this fix, unchanged behavior for that case.
        if xsrf_token:
            fields["at"] = xsrf_token
        return urlencode(fields)
    except Exception:
        return None


async def fetch_next_page(
    page: Page,
    captured_request: dict,
    pagination_token: str,
    reqid: int,
    page_size: int | None = None,
    xsrf_token: str | None = None,
) -> tuple[dict | None, str | None]:
    """One RPC call, bypassing DOM/scroll entirely. Returns
    (parsed {"pagination_token", "reviews"} dict, None) on success, or
    (None, reason) on failure — the reason string is for the caller to log,
    since silently swallowing why a call failed (rate limit? stale/rejected
    bgkey? malformed request? genuinely no more pages?) made a real run's
    "RPC-paging stopped" impossible to diagnose. `page_size` overrides the
    captured template's page-size value (10 in every real request seen so
    far) — an experiment to see whether Google honors a larger request or
    silently caps/rejects it; leave None to keep the template's own value."""
    post_data = build_post_data(captured_request["postData"], pagination_token, page_size, xsrf_token)
    if post_data is None:
        return None, "could not build POST body from the captured request template"

    url = re.sub(r"_reqid=\d+", f"_reqid={reqid}", captured_request["url"])
    # Confirmed live (2026-10-19), comparing a real second-page browser
    # request against ours: the "source-path" query param reflects the
    # page's CURRENT url (zoom level, sort-state segments like !5m1!1e1)
    # at the moment of that specific call — not a static value. The
    # captured template's source-path is a one-time snapshot from the sort
    # click, already stale by the time a later page is requested (same
    # well-formed-but-empty-response symptom as the stale correlation id
    # above — no error, just silently the wrong page). Refreshed here from
    # the live page on every call instead of trusting the template's copy.
    try:
        live_path = urlsplit(page.url).path
        # safe="!" — matches the captured template's own encoding style
        # (every "/", "@", ",", ":", "=" percent-encoded, but literal "!"
        # left alone, e.g. "...%2Fdata%3D!4m8!3m7!1s...").
        url = re.sub(r"source-path=[^&]*", f"source-path={quote(live_path, safe='!')}", url)
    except Exception:
        pass  # best-effort — fall back to the template's source-path if this fails
    headers = {k: v for k, v in captured_request["headers"].items() if k.lower() not in ("content-length", "cookie")}

    # Confirmed on a real run: a stop was actually an HTTP 502 (Google's own
    # server error page, "Error 502 (Server Error)!!1") — a transient
    # infrastructure hiccup, not a rejection tied to anything about this
    # request (bgkey, page-size, etc). That's very likely recoverable with a
    # short retry rather than giving up on RPC-paging entirely for the rest
    # of the run.
    last_error: str | None = None
    for attempt in range(1, 4):
        try:
            response = await page.request.post(url, headers=headers, data=post_data)
            if response.status in (502, 503, 504):
                last_error = f"HTTP {response.status} (attempt {attempt}/3)"
                await asyncio.sleep(1.5 * attempt)
                continue
            if response.status != 200:
                body_preview = (await response.text())[:300]
                return None, f"HTTP {response.status}: {body_preview}"
            body = await response.body()
            try:
                parsed = parse_batch_execute_body(body.decode("utf-8"))
            except UnexpectedResponseFormatError as exc:
                # Deterministic, not transient — every retry would hit the
                # exact same format mismatch. Fail fast instead of burning
                # 3 attempts' worth of backoff (up to ~9s) on something a
                # retry can never fix.
                return None, f"Google's response format may have changed: {exc}"
            if not parsed:
                body_preview = body.decode("utf-8", errors="replace")[:300]
                return None, f"response body didn't parse as the expected format: {body_preview}"
            return parsed, None
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            await asyncio.sleep(1.5 * attempt)

    return None, f"gave up after 3 attempts, last error: {last_error}"
