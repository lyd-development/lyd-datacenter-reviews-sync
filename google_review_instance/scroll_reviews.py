"""Scrolls the review feed and collects results from the intercepted network
responses. Also captures the raw outgoing batchexecute request once per run
(see main.py) as a still-open, separate experiment about whether that RPC
could be replayed without a real browser — unrelated to the
review-collection logic below.
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any, Awaitable, Callable

from apify import Actor
from playwright.async_api import Page

from .maps_api import UnexpectedResponseFormatError, parse_batch_execute_body
from .review_link import build_review_link


async def random_delay(min_ms: int, max_ms: int) -> None:
    await asyncio.sleep((min_ms + random.random() * (max_ms - min_ms)) / 1000)


def create_review_collector(page: Page, on_request_captured: Callable[[dict], Awaitable[None]] | None = None) -> dict:
    reviews: dict[str, dict[str, Any]] = {}
    state: dict[str, Any] = {"pagination_token": None}
    captured = {"done": False}
    # Logged once per run, not once per response — an actual format change
    # affects every response identically, so logging every occurrence would
    # just spam the run log without adding information.
    format_error_logged = {"done": False}

    def on_request(request: Any) -> None:
        if captured["done"] or on_request_captured is None:
            return
        url = request.url
        if "batchexecute" not in url or "qv9Egd" not in url:
            return
        captured["done"] = True
        asyncio.create_task(
            on_request_captured(
                {
                    "url": url,
                    "method": request.method,
                    "headers": request.headers,
                    "postData": request.post_data,
                }
            )
        )

    async def on_response(response: Any) -> None:
        url = response.url
        if "batchexecute" not in url or "qv9Egd" not in url:
            return
        try:
            body_bytes = await response.body()
            body = body_bytes.decode("utf-8")
            parsed = parse_batch_execute_body(body)
            if not parsed:
                return
            for review in parsed["reviews"]:
                reviews[review["review_id"]] = review
            state["pagination_token"] = parsed.get("pagination_token")
        except UnexpectedResponseFormatError as exc:
            # Loud and specific — this is the exact class of bug that went
            # unnoticed silently for a while (see maps_api.py's
            # KNOWN_FRAME_IDENTIFIERS docstring). Every future occurrence
            # should show up clearly in the run log instead of just
            # manifesting as a mysteriously empty/failed run.
            if not format_error_logged["done"]:
                format_error_logged["done"] = True
                Actor.log.error(f"Google's batchexecute response format may have changed: {exc}")
        except Exception:
            pass  # genuinely malformed/unexpected data beyond just the frame identifier — not actionable here

    if on_request_captured is not None:
        page.on("request", on_request)
    page.on("response", on_response)
    return {"reviews": reviews, "state": state}


async def get_review_scroll_container(page: Page):
    return await page.evaluate_handle(
        """() => {
            const card = document.querySelector("[data-review-id]");
            let el = card ? card.parentElement : null;
            while (el) {
                const style = window.getComputedStyle(el);
                if ((style.overflowY === "auto" || style.overflowY === "scroll") && el.scrollHeight > el.clientHeight) {
                    return el;
                }
                el = el.parentElement;
            }
            return null;
        }"""
    )


def _sort_newest_first(items: list[dict]) -> list[dict]:
    return sorted(items, key=lambda r: r["review_timestamp"], reverse=True)


async def scroll_and_collect_reviews(
    page: Page,
    collector: dict[str, dict[str, Any]],
    *,
    max_scroll_attempts_without_new_reviews: int,
    scroll_delay_min_ms: int,
    scroll_delay_max_ms: int,
    max_reviews: int | None = None,
    location_url: str | None = None,
    on_progress: Callable[[int, int], None] | None = None,
    on_flush: Callable[[list[dict]], Awaitable[None]] | None = None,
    flush_interval_ms: int | None = None,
    flush_batch_size: int | None = None,
    exclude_from_cap: set[str] | None = None,
    sort_output: bool = False,
    already_flushed_ids: set[str] | None = None,
) -> dict:
    """`already_flushed_ids` — seeds the "already pushed to the dataset" set,
    for when a caller (main.py's RPC-pagination fallback path) already
    pushed some reviews before this function was called, so they don't get
    pushed a second time."""
    feed = await get_review_scroll_container(page)
    if not feed:
        raise RuntimeError("Could not locate the review list's scroll container")

    exclude_from_cap = exclude_from_cap or set()
    reviews = collector["reviews"]
    links_built: set[str] = set()
    flushed_ids: set[str] = set(already_flushed_ids or ())
    previous_count = 0
    no_new_count = 0
    reached_cap = False
    last_flush_at = time.monotonic()

    while no_new_count < max_scroll_attempts_without_new_reviews:
        await feed.evaluate("(el) => el.scrollTo(0, Math.max(0, el.scrollHeight - 200))")
        await page.wait_for_timeout(150)
        await feed.evaluate("(el) => el.scrollTo(0, el.scrollHeight)")
        await random_delay(scroll_delay_min_ms, scroll_delay_max_ms)

        for review_id, record in list(reviews.items()):
            if review_id not in links_built:
                record["review_link"] = build_review_link(location_url, review_id)
                links_built.add(review_id)

        new_count = sum(1 for rid in reviews if rid not in exclude_from_cap)

        if on_progress:
            on_progress(len(reviews), new_count)

        if on_flush:
            since_flush = [r for rid, r in reviews.items() if rid not in flushed_ids]
            time_elapsed = bool(flush_interval_ms) and (time.monotonic() - last_flush_at) * 1000 >= flush_interval_ms
            enough_new = bool(flush_batch_size) and len(since_flush) >= flush_batch_size
            if since_flush and (time_elapsed or enough_new):
                await on_flush(_sort_newest_first(since_flush) if sort_output else since_flush)
                flushed_ids.update(r["review_id"] for r in since_flush)
                last_flush_at = time.monotonic()

        if max_reviews and new_count >= max_reviews:
            reached_cap = True
            break

        if len(reviews) == previous_count:
            no_new_count += 1
        else:
            no_new_count = 0
        previous_count = len(reviews)

    if on_flush:
        remaining = [r for rid, r in reviews.items() if rid not in flushed_ids]
        if remaining:
            await on_flush(_sort_newest_first(remaining) if sort_output else remaining)

    final_reviews = list(reviews.values())
    return {
        "reviews": _sort_newest_first(final_reviews) if sort_output else final_reviews,
        "reached_cap": reached_cap,
    }
