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
import json
import re
from typing import Any
from urllib.parse import parse_qs, urlencode

from playwright.async_api import Page

from .maps_api import parse_batch_execute_body

CID_PAIR_PATTERN = re.compile(r"!1s(0x[0-9a-f]+:0x[0-9a-f]+)", re.IGNORECASE)


def extract_cid_pair(location_url: str) -> str | None:
    match = CID_PAIR_PATTERN.search(location_url)
    return match.group(1) if match else None


def _set_pagination_params(args: list[Any], token: str, page_size: int | None) -> list[Any]:
    """args[1] is [pageSize, paginationToken] in every captured request —
    the cursor always changes between pages; page_size is normally left as
    whatever the captured template already had (10, in every real request
    seen so far — that's Google's own client's choice, not ours) unless a
    caller explicitly overrides it to test whether a bigger page is honored.
    Everything else (sort context, CID, client date info) is reused verbatim
    from the captured template. Unverified assumption — see module
    docstring."""
    args = json.loads(json.dumps(args))  # deep copy, don't mutate the template
    if isinstance(args, list) and len(args) > 1 and isinstance(args[1], list) and len(args[1]) >= 2:
        size = page_size if page_size is not None else args[1][0]
        args[1] = [size, token]
    return args


def build_post_data(template_post_data: str, pagination_token: str, page_size: int | None = None) -> str | None:
    try:
        parsed_qs = parse_qs(template_post_data)
        f_req = json.loads(parsed_qs["f.req"][0])
        inner_args = json.loads(f_req[0][0][1])
        f_req[0][0][1] = json.dumps(_set_pagination_params(inner_args, pagination_token, page_size))
        return urlencode({"f.req": json.dumps(f_req)})
    except Exception:
        return None


async def fetch_next_page(
    page: Page,
    captured_request: dict,
    pagination_token: str,
    reqid: int,
    page_size: int | None = None,
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
    post_data = build_post_data(captured_request["postData"], pagination_token, page_size)
    if post_data is None:
        return None, "could not build POST body from the captured request template"

    url = re.sub(r"_reqid=\d+", f"_reqid={reqid}", captured_request["url"])
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
            parsed = parse_batch_execute_body(body.decode("utf-8"))
            if not parsed:
                body_preview = body.decode("utf-8", errors="replace")[:300]
                return None, f"response body didn't parse as the expected format: {body_preview}"
            return parsed, None
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            await asyncio.sleep(1.5 * attempt)

    return None, f"gave up after 3 attempts, last error: {last_error}"
