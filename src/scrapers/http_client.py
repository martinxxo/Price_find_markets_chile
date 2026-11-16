"""Shared HTTP helpers and price parsing utilities for store connectors."""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 12
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/129.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "es-CL,es;q=0.9,en;q=0.8",
}


def fetch_json(
    url: str,
    params: Optional[dict[str, Any]] = None,
    headers: Optional[dict[str, str]] = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> Optional[dict[str, Any] | list[Any]]:
    """Fetch JSON safely without crashing the full scan on store blocks or timeouts."""
    merged_headers = dict(DEFAULT_HEADERS)
    if headers:
        merged_headers.update(headers)

    try:
        response = requests.get(
            url,
            params=params,
            headers=merged_headers,
            timeout=timeout,
        )
        if response.status_code in (403, 429, 503):
            logger.warning("Store returned HTTP %s for %s", response.status_code, url)
            return None
        response.raise_for_status()
        return response.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning("Failed fetching %s: %s", url, exc)
        return None


def parse_clp_price(raw_value: Any) -> Optional[int]:
    """Parse Chilean peso strings or numeric values (e.g. '$ 199.990', 199990) to int."""
    if raw_value is None or isinstance(raw_value, bool):
        return None
    if isinstance(raw_value, (int, float)):
        val = int(round(raw_value))
        return val if val > 0 else None
    text = str(raw_value).strip()
    if not text:
        return None
    digits = re.sub(r"[^\d]", "", text)
    if not digits:
        return None
    val = int(digits)
    return val if val > 0 else None
