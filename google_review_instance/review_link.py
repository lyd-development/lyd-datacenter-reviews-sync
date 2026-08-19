"""Builds a direct link to a single review — pure string construction, no
browser/DB coupling.
"""

from __future__ import annotations

import re
from urllib.parse import quote

LAT_LNG_PATTERN = re.compile(r"@(-?\d+\.\d+),(-?\d+\.\d+)")
CID_HEX_PATTERN = re.compile(r"!1s0x[0-9a-f]+:(0x[0-9a-f]+)", re.IGNORECASE)


def build_review_link(location_url: str | None, review_id: str | None) -> str | None:
    if not location_url or not review_id:
        return None

    lat_lng_match = LAT_LNG_PATTERN.search(location_url)
    cid_match = CID_HEX_PATTERN.search(location_url)
    if not lat_lng_match or not cid_match:
        return None

    lat, lng = lat_lng_match.group(1), lat_lng_match.group(2)
    cid_hex = cid_match.group(1)
    encoded_id = quote(review_id, safe="")

    return (
        f"https://www.google.com/maps/reviews/@{lat},{lng},14z/data="
        f"!3m1!1e3!4m6!14m5!1m4!2m3!1s{encoded_id}!2m1!1s0x0:{cid_hex}"
    )
