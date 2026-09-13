"""Snapshot api.amboss.space (GraphQL).

Endpoint and auth follow the Amboss developer reference
(https://docs.amboss.tech/developer): the Space schema lives at
https://api.amboss.space/graphql and takes `Authorization: Bearer <key>`.
The Magma offer fields used below (base_fee, base_fee_cap, fee_rate,
fee_rate_cap, min_size, max_size, total_size, min_block_length) are the ones
documented for the marketplace offer object. LNR is Amboss's own benchmark: the
cost of buying a channel expressed over a one-year term of ~52,560 blocks, so
the values it returns are already annualised and are NOT term rates.

AMBOSS_API_KEY is read from the environment (.env). It is OPTIONAL: at the last
run (2026-09-13) every query below answered unauthenticated.

Counter-intuitive but reproducible: on a free-tier key, sending the
Authorization header on the two time-windowed queries (order_details,
lnr_series) makes the API answer "Cannot query data older than 7 days", while
the anonymous path returns the full history. The remaining operations return
byte-identical payloads either way. Which operations carry the key is therefore
a config decision (sources.amboss.authenticate), not a guess made at runtime,
and the choice is recorded per file in the manifest as `authenticated`.

Collected:
  getOffers(offerType: CHANNEL)      live open channel-lease offers (the ask side
                                     of the Magma market)
  getMarketMetrics.order_details     executed leases: size, block_duration and
                                     the annualised rate (lnr)
  getMarketMetrics.lnr_curve_buckets rate term structure by channel-size bucket
  getMarketMetrics.lnr_series        aggregate market rate series
  getNetworkMetrics                  network-level totals (node counts, channel
                                     count, aggregate and median capacity, median
                                     fee rate) as an independent cross-check on
                                     mempool.space
  getRankLists                       node universe by capacity / channel count.
                                     NOTE: this query returns only the top 20
                                     pubkeys per list, which caps the node
                                     universe for the ppm distribution.
  getNode.graph_info.fee_buckets     per-node histogram of outbound fee rates in
                                     ppm, for the ranked nodes

The ppm distribution is assembled from per-node histograms because Amboss
exposes no single "distribution across all nodes" query, and because the
histogram is the only shape in which it publishes fee rates — raw per-channel
ppm values are not available, so the paper's percentiles are interpolated. Every
node response is stored verbatim in one file per run, keyed by pubkey; nothing
is dropped or pre-aggregated at fetch time.
"""

from __future__ import annotations

import json
import sys

from common import (
    FetchError,
    load_api_key,
    load_config,
    post_graphql,
    save_raw,
    utc_now,
)

SOURCE = "api.amboss.space"

Q_OFFERS = """
query Offers($offerType: MarketOfferType) {
  getOffers(offerType: $offerType) {
    list {
      id
      account
      amboss_fee_rate
      base_fee
      base_fee_cap
      fee_rate
      fee_rate_cap
      min_size
      max_size
      total_size
      min_block_length
      offer_type
      onchain_multiplier
      onchain_priority
      seller_score
      side
      status
      tags { name }
      conditions { condition operator value }
    }
  }
}
"""

Q_ORDER_DETAILS = """
query OrderDetails($from: String!) {
  getMarketMetrics {
    order_details(from: $from) {
      date
      size
      block_duration
      lnr
      lny
      status
    }
  }
}
"""

Q_LNR_CURVE_BUCKETS = """
query LnrCurveBuckets {
  getMarketMetrics {
    lnr_curve_buckets {
      bucket_min
      bucket_max
      curves { month series { duration lnr } }
    }
  }
}
"""

Q_LNR_SERIES = """
query LnrSeries($from: String!, $period: LnrSearchPeriod!) {
  getMarketMetrics {
    lnr_series(from: $from, period: $period) {
      date
      lnr
      lnr_cost
      lnr_yield
      lny
    }
  }
}
"""

# Field shape confirmed by GraphQL introspection against the live schema on
# 2026-09-13 (getNetworkMetrics -> GeneralNetworkMetrics.historical_snapshots).
# This is an INDEPENDENT reading of the same network totals mempool.space
# publishes, collected so the two can be compared rather than trusted singly.
# Note what is absent: the snapshot carries a UUID `id` but no as-of timestamp,
# so Amboss publishes no date for these aggregates and the fetch time is the
# only date available for them.
Q_NETWORK_METRICS = """
query NetworkMetrics {
  getNetworkMetrics {
    id
    historical_snapshots {
      id
      nodes { total active }
      channels {
        channel_metrics { count sum mean median min max }
        fee_rate_metrics { mean median }
        base_fee_metrics { mean median }
      }
    }
  }
}
"""

Q_RANK_LISTS = """
query RankLists {
  getRankLists {
    capacity { pubkey rank }
    channels { pubkey rank }
  }
}
"""

Q_NODE_FEE_BUCKETS = """
query NodeFeeBuckets($pubkey: String!) {
  getNode(pubkey: $pubkey) {
    graph_info {
      last_update
      metrics { capacity channels capacity_rank channels_rank }
      fee_buckets {
        local_buckets { index bucket_label min_limit max_limit amount_channels total_capacity }
        remote_buckets { index bucket_label min_limit max_limit amount_channels total_capacity }
      }
    }
  }
}
"""


def _auth_for(cfg: dict, operation: str, api_key: str | None) -> str | None:
    """Return the key to send for `operation`, honouring config.

    Absent config entry defaults to sending the key when one exists.
    """
    policy = (cfg["sources"]["amboss"].get("authenticate") or {})
    if not api_key:
        return None
    return api_key if policy.get(operation, True) else None


def _fetch_and_store(
    endpoint_name: str,
    query: str,
    cfg: dict,
    api_key: str | None,
    *,
    variables: dict | None = None,
):
    ep = cfg["sources"]["amboss"]["endpoint"]
    key = _auth_for(cfg, endpoint_name, api_key)
    mode = "authenticated" if key else "anonymous"
    print(f"- {endpoint_name} [{mode}]")
    fetched_at = utc_now()
    parsed, raw, status = post_graphql(
        ep, query, cfg, variables=variables, api_key=key
    )
    save_raw(
        SOURCE,
        endpoint_name,
        raw,
        url=ep,
        http_status=status,
        fetched_at=fetched_at,
        extra={
            "graphql_operation": endpoint_name,
            "graphql_variables": variables or {},
            "authenticated": bool(key),
        },
    )
    return parsed


def _fetch_node_fee_buckets(cfg: dict, api_key: str | None, pubkeys: list[str]) -> None:
    """Sweep per-node fee histograms and store every response verbatim."""
    ep = cfg["sources"]["amboss"]["endpoint"]
    key = _auth_for(cfg, "node_fee_buckets", api_key)
    mode = "authenticated" if key else "anonymous"
    print(f"- node_fee_buckets for {len(pubkeys)} nodes [{mode}] "
          f"(~{len(pubkeys) * float(cfg['http']['pause_between_requests_seconds']):.0f}s)")

    fetched_at = utc_now()
    responses: dict[str, object] = {}
    failures = 0

    for i, pubkey in enumerate(pubkeys, start=1):
        try:
            parsed, _, _ = post_graphql(
                ep,
                Q_NODE_FEE_BUCKETS,
                cfg,
                variables={"pubkey": pubkey},
                api_key=key,
                allow_graphql_errors=True,
            )
        except FetchError as exc:
            failures += 1
            responses[pubkey] = {"fetch_error": str(exc)}
            print(f"  [{i}/{len(pubkeys)}] {pubkey[:16]}… FAILED: {exc}", file=sys.stderr)
            continue

        if isinstance(parsed, dict) and parsed.get("errors"):
            failures += 1
            print(f"  [{i}/{len(pubkeys)}] {pubkey[:16]}… graphql error", file=sys.stderr)
        responses[pubkey] = parsed

        if i % 20 == 0:
            print(f"  [{i}/{len(pubkeys)}] …")

    if failures == len(pubkeys):
        raise FetchError(
            "every per-node fee-bucket request failed; refusing to store an "
            "empty distribution snapshot"
        )
    if failures:
        print(f"  {failures}/{len(pubkeys)} nodes returned errors (recorded in the file)")

    document = {
        "_note": (
            "One Amboss GraphQL response body per pubkey, stored verbatim. "
            "Keys are node pubkeys; values are the unmodified response documents."
        ),
        "_query": Q_NODE_FEE_BUCKETS,
        "_requested_pubkeys": pubkeys,
        "_failures": failures,
        "responses": responses,
    }
    raw = json.dumps(document, ensure_ascii=False).encode("utf-8")
    save_raw(
        SOURCE,
        "node_fee_buckets",
        raw,
        url=ep,
        http_status=200,
        fetched_at=fetched_at,
        extra={
            "graphql_operation": "getNode.graph_info.fee_buckets",
            "node_count": len(pubkeys),
            "failed_nodes": failures,
            "authenticated": bool(key),
        },
    )


def main() -> int:
    cfg = load_config()
    amboss = cfg["sources"]["amboss"]
    api_key = load_api_key("AMBOSS_API_KEY")

    if api_key:
        policy = amboss.get("authenticate") or {}
        anon = sorted(op for op, use in policy.items() if not use)
        print("[fetch_amboss] AMBOSS_API_KEY found")
        if anon:
            print(
                "[fetch_amboss] sent anonymously by config (free-tier 7-day "
                f"window): {', '.join(anon)}"
            )
    else:
        print(
            "[fetch_amboss] no AMBOSS_API_KEY set; querying anonymously "
            "(supported at last check — see README)"
        )

    _fetch_and_store(
        "magma_offers", Q_OFFERS, cfg, api_key, variables={"offerType": "CHANNEL"}
    )
    _fetch_and_store(
        "magma_order_details",
        Q_ORDER_DETAILS,
        cfg,
        api_key,
        variables={"from": str(amboss["orders_from"])},
    )
    _fetch_and_store("network_metrics", Q_NETWORK_METRICS, cfg, api_key)
    _fetch_and_store("magma_lnr_curve_buckets", Q_LNR_CURVE_BUCKETS, cfg, api_key)
    _fetch_and_store(
        "magma_lnr_series",
        Q_LNR_SERIES,
        cfg,
        api_key,
        variables={
            "from": str(amboss["lnr_from"]),
            "period": str(amboss["lnr_period"]),
        },
    )

    ranks = _fetch_and_store("rank_lists", Q_RANK_LISTS, cfg, api_key)
    capacity_ranks = (
        ((ranks or {}).get("data") or {}).get("getRankLists") or {}
    ).get("capacity")
    if not capacity_ranks:
        raise FetchError(
            "getRankLists returned no capacity ranking; cannot choose the node "
            "universe for the ppm distribution"
        )

    top_n = int(amboss["top_nodes_for_fee_distribution"])
    ordered = sorted(capacity_ranks, key=lambda item: item["rank"])[:top_n]
    _fetch_node_fee_buckets(cfg, api_key, [item["pubkey"] for item in ordered])

    print("[fetch_amboss] done")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except FetchError as exc:
        print(f"[fetch_amboss] FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
