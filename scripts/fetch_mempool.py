"""Snapshot the mempool.space public REST API (no API key required).

Every path below is taken from the published REST reference at
https://mempool.space/docs/api/rest — nothing here is inferred from memory or
discovered by probing:

  /api/v1/fees/recommended                       on-chain fee tiers, integer sat/vB
  /api/v1/fees/precise                           same tiers to 0.1 sat/vB
  /api/v1/lightning/statistics/latest            network totals + fee averages
  /api/v1/lightning/statistics/<interval>        historical series
  /api/v1/lightning/nodes/rankings               top nodes by capacity and by channels
  /api/v1/lightning/nodes/rankings/liquidity     top 100 by aggregate capacity
  /api/v1/lightning/nodes/rankings/connectivity  top 100 by channel count

Two limitations are carried into the README rather than worked around:

1. The documented interval set for /lightning/statistics/ is
   latest|24h|3d|1w|1m|3m|6m|1y|2y|3y. Longer values are not part of the
   contract, so the configured interval is validated against that list and an
   unknown one is a hard error.
2. The historical series returns only channel_count / total_capacity / node
   counts. The fee fields (avg_fee_rate, med_fee_rate, avg_base_fee_mtokens,
   med_base_fee_mtokens) exist ONLY on /statistics/latest, so mempool.space
   offers no historical ppm series at all.
"""

from __future__ import annotations

import sys

from common import FetchError, get_json, load_config, save_raw, utc_now

SOURCE = "mempool.space"

# https://mempool.space/docs/api/rest -> "GET Network Statistics".
DOCUMENTED_STATISTICS_INTERVALS = (
    "latest", "24h", "3d", "1w", "1m", "3m", "6m", "1y", "2y", "3y",
)


def main() -> int:
    cfg = load_config()
    base = cfg["sources"]["mempool"]["base_url"].rstrip("/")
    interval = str(cfg["sources"]["mempool"]["history_interval"])

    if interval not in DOCUMENTED_STATISTICS_INTERVALS:
        raise FetchError(
            f"history_interval={interval!r} is not a documented interval. "
            f"Allowed: {', '.join(DOCUMENTED_STATISTICS_INTERVALS)}. "
            "Undocumented values may answer today and stop answering tomorrow; "
            "the snapshot must rest on the published contract."
        )
    if interval == "latest":
        raise FetchError(
            "history_interval='latest' is not a history; pick one of "
            f"{', '.join(i for i in DOCUMENTED_STATISTICS_INTERVALS if i != 'latest')}."
        )

    targets = [
        ("fees_recommended", f"{base}/api/v1/fees/recommended"),
        ("fees_precise", f"{base}/api/v1/fees/precise"),
        ("lightning_statistics_latest", f"{base}/api/v1/lightning/statistics/latest"),
        (
            f"lightning_statistics_{interval}",
            f"{base}/api/v1/lightning/statistics/{interval}",
        ),
        ("lightning_nodes_rankings", f"{base}/api/v1/lightning/nodes/rankings"),
        (
            "lightning_nodes_rankings_liquidity",
            f"{base}/api/v1/lightning/nodes/rankings/liquidity",
        ),
        (
            "lightning_nodes_rankings_connectivity",
            f"{base}/api/v1/lightning/nodes/rankings/connectivity",
        ),
    ]

    print(f"[fetch_mempool] {len(targets)} endpoints from {base}")
    for endpoint, url in targets:
        print(f"- {endpoint}")
        fetched_at = utc_now()
        _, raw, status = get_json(url, cfg)
        save_raw(
            SOURCE,
            endpoint,
            raw,
            url=url,
            http_status=status,
            fetched_at=fetched_at,
        )

    print("[fetch_mempool] done")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except FetchError as exc:
        print(f"[fetch_mempool] FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
