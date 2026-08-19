"""Parses Google's undocumented batchexecute RPC response format for review
data — full field parity (author info, ratings, text, translations,
sub-ratings, owner replies). Field indices were derived empirically, not
from any official documentation.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any


def _get(seq: Any, index: int, default: Any = None) -> Any:
    if isinstance(seq, (list, tuple)) and 0 <= index < len(seq):
        return seq[index]
    return default


def micros_to_datetime(micros: Any) -> datetime | None:
    if not isinstance(micros, (int, float)):
        return None
    millis = round(micros / 1000)
    return datetime.fromtimestamp(millis / 1000, tz=timezone.utc)


def parse_batch_execute_body(body: str) -> dict | None:
    body_start = body.find("\n\n")
    if body_start == -1:
        return None
    lines = [line for line in body[body_start + 2 :].split("\n") if line.strip()]
    if len(lines) < 2:
        return None

    outer = json.loads(lines[1])
    frame = next((f for f in outer if _get(f, 1) == "/MapsUgcPostService.ListUgcPosts"), None)
    if not frame:
        return None

    if not isinstance(_get(frame, 2), str):
        # Confirmed on a real run: a request that deviates from what
        # Google's own client actually sends (e.g. a bumped page-size) can
        # come back with a "frame" that matched the RPC name but doesn't
        # carry real data at index 2 (rejected/error response) — surfacing
        # that clearly here beats a cryptic json.loads(None) TypeError from
        # the caller.
        raise ValueError(
            f"batchexecute frame[2] wasn't a JSON string (got {frame[2]!r}) — likely a rejected request. "
            f"Full frame: {frame!r}. Outer envelope: {outer!r}"[:1000]
        )

    inner = json.loads(frame[2])
    pagination_token = _get(inner, 1)
    raw_reviews = _get(inner, 2, [])

    reviews = [parse_review_record(_get(wrapped, 0)) for wrapped in raw_reviews]
    return {"pagination_token": pagination_token, "reviews": [r for r in reviews if r]}


def parse_guided_review(guided: Any) -> tuple[dict | None, dict | None]:
    if not isinstance(guided, list) or not guided:
        return None, None

    sub_ratings: dict[str, float] = {}
    review_attributes: dict[str, str] = {}

    for item in guided:
        label = _get(item, 5)
        if not label:
            continue

        rating_block = _get(item, 11)
        if isinstance(rating_block, list) and rating_block and isinstance(rating_block[0], (int, float)):
            sub_ratings[label.lower()] = rating_block[0]
            continue

        answer_block = _get(_get(_get(item, 2), 0), 0) or _get(_get(_get(item, 3), 0), 0)
        answer = (_get(answer_block, 4) or _get(answer_block, 1)) if answer_block else None
        if answer:
            review_attributes[label] = answer

    return (sub_ratings or None), (review_attributes or None)


def parse_review_record(r: Any) -> dict | None:
    if not r or not _get(r, 0):
        return None

    review_id = r[0]
    meta = _get(r, 1, [])
    other_array = _get(r, 2, [])
    owner_reply = _get(r, 3)

    author_block = _get(_get(meta, 4), 5, []) or []
    author_name = _get(author_block, 0) or "Unknown"
    author_image = _get(author_block, 1)
    author_link = _get(_get(author_block, 2), 0)
    author_id = _get(author_block, 3) or review_id
    author_reviews_count = _get(author_block, 5)
    if not isinstance(author_reviews_count, (int, float)):
        author_reviews_count = None
    author_photos_count = _get(author_block, 6)
    if not isinstance(author_photos_count, (int, float)):
        author_photos_count = None
    is_local_guide = _get(_get(author_block, 8), 0)
    if not isinstance(is_local_guide, bool):
        is_local_guide = None

    review_rating = _get(_get(other_array, 0), 0)
    review_timestamp = micros_to_datetime(_get(meta, 2))

    # Some venues (hotels especially) aggregate reviews from other platforms
    # alongside native Google ones — the card shows "on Tripadvisor"/"on
    # Trip.com" instead of "on Google" for those. meta[13] carries
    # [sourceName, iconUrl, null, null, N] for the native-Google case
    # (confirmed against real captured RPC data); assumed to hold the
    # aggregated platform's name the same way for non-Google reviews, but
    # that specific shape hasn't been confirmed against a real Tripadvisor/
    # Trip.com-sourced review yet — falls back to "Google" (the default for
    # venues, like restaurants, that don't aggregate at all) whenever this
    # isn't present or isn't a string, per explicit instruction rather than
    # guessing at an unconfirmed shape.
    source_name = _get(_get(meta, 13), 0)
    review_source = source_name if isinstance(source_name, str) and source_name else "Google"

    review_text_block = _get(other_array, 15)
    review_text_original = _get(_get(review_text_block, 0), 0)
    review_text_translated = _get(_get(review_text_block, 1), 0)
    review_language = _get(_get(other_array, 14), 0)

    photo_entries = _get(other_array, 2, []) or []
    photo_urls = []
    for entry in photo_entries:
        url = _get(_get(_get(entry, 1), 6), 0)
        if url:
            photo_urls.append(url)
    review_img_url = photo_urls[0] if photo_urls else None
    review_img_urls = photo_urls or None

    owner_answer = None
    owner_answer_timestamp = None
    if owner_reply:
        reply_text_block = _get(owner_reply, 14)
        owner_answer = _get(_get(reply_text_block, 0), 0)
        owner_answer_timestamp = micros_to_datetime(_get(owner_reply, 1))

    if not review_rating or not review_timestamp:
        return None  # required fields missing — skip, don't guess

    sub_ratings, review_attributes = parse_guided_review(_get(other_array, 6))

    return {
        "review_id": review_id,
        "author_id": author_id,
        "author_name": author_name,
        "author_link": author_link,
        "author_image": author_image,
        "author_reviews_count": author_reviews_count,
        "author_photos_count": author_photos_count,
        "author_is_local_guide": is_local_guide,
        "review_text_original": review_text_original,
        "review_text_translated": review_text_translated,
        "review_language": review_language,
        "review_rating": review_rating,
        "review_timestamp": review_timestamp,
        "review_source": review_source,
        "review_link": None,  # filled in by review_link.py's build_review_link()
        "review_img_url": review_img_url,
        "review_img_urls": review_img_urls,
        "owner_answer": owner_answer,
        "owner_answer_timestamp": owner_answer_timestamp,
        "sub_ratings": sub_ratings,
        "review_attributes": review_attributes,
    }
