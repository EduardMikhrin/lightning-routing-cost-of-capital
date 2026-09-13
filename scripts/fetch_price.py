"""Snapshot the BTC price from CoinGecko.

One request for every quote currency at once, so that the USD and UAH figures
in the paper come from the same instant. Used to convert the USD-denominated
OPEX assumption into BTC and to report results in fiat.
"""

from __future__ import annotations

import sys
from urllib.parse import urlencode

from common import FetchError, get_json, load_config, save_raw, utc_now

SOURCE = "api.coingecko.com"


def main() -> int:
    cfg = load_config()
    cg = cfg["sources"]["coingecko"]
    base = cg["base_url"].rstrip("/")
    coin_id = cg["coin_id"]
    currencies = [str(c).lower() for c in cg["vs_currencies"]]

    query = urlencode(
        {
            "ids": coin_id,
            "vs_currencies": ",".join(currencies),
            "include_last_updated_at": "true",
        }
    )
    url = f"{base}/api/v3/simple/price?{query}"

    print(f"[fetch_price] {coin_id} -> {', '.join(currencies)}")
    fetched_at = utc_now()
    parsed, raw, status = get_json(url, cfg)

    # Fail loudly rather than storing a snapshot that the build cannot use.
    quotes = parsed.get(coin_id) if isinstance(parsed, dict) else None
    if not isinstance(quotes, dict):
        raise FetchError(f"unexpected CoinGecko payload shape: {str(parsed)[:300]}")
    missing = [c for c in currencies if c not in quotes]
    if missing:
        raise FetchError(
            f"CoinGecko returned no quote for {missing} (got {list(quotes)}). "
            "Check the currency codes in config.yaml."
        )

    save_raw(
        SOURCE,
        "simple_price",
        raw,
        url=url,
        http_status=status,
        fetched_at=fetched_at,
    )
    for currency in currencies:
        print(f"  {coin_id}/{currency.upper()} = {quotes[currency]:,}")

    print("[fetch_price] done")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except FetchError as exc:
        print(f"[fetch_price] FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
