"""Turn the raw snapshots into the paper's derived tables and figure.

Nothing is fetched here. Every input is the newest file recorded in
data/raw/manifest.jsonl for a given (source, endpoint), and common.latest_snapshot
re-hashes it before use, so a hand-edited raw file stops the build instead of
quietly changing a published number.

Every model parameter comes from config.yaml. The only constants in this file
are unit conversions (1 BTC = 1e8 sat, 12 months = 1 year).

Model
-----
    APY_routing = (F_earned - F_rebalancing - OnChain_amortised - OPEX) / K_locked

    V             = u * K_locked * days_per_year        annual routed volume
    F_earned      = ppm * 1e-6 * V
    F_rebalancing = r_reb * F_earned
    OnChain_amort = (K_locked / channel_size)
                    * (open_vbytes + close_vbytes) * sat_per_vB
                    * (12 / L_months)
    OPEX          = opex_usd_per_year / BTCUSD

APY is linear in u, so break-even utilisation is solved in closed form rather
than searched on the grid:

    u* = (coc + (OnChain_amort + OPEX) / K_locked) / (ppm * 1e-6 * days * (1 - r_reb))

Outputs (data/derived/)
-----------------------
    network_snapshot.csv          mempool.space headline Lightning figures
    lightning_history.csv         the documented historical series
    ppm_distribution.csv          outbound fee-rate percentiles (Amboss histogram)
    table2_cost_of_capital.csv    observed price of capital, annualised
    table3_scenarios.csv          the model at three node sizes
    sensitivity.csv               APY over the (u, ppm) grid + break-even u
    _sources.json                 every raw file this build consumed, with hashes

    figures/fig1_apy_vs_utilization.png
    figures/fig1_apy_vs_utilization.csv
"""

from __future__ import annotations

import json
import math
import statistics
import sys
from datetime import datetime, timezone

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

from common import (
    DERIVED_DIR,
    FIGURES_DIR,
    FetchError,
    REPO_ROOT,
    iso_utc,
    latest_snapshot,
    load_config,
    utc_now,
)

SAT_PER_BTC = 100_000_000
MONTHS_PER_YEAR = 12

MEMPOOL = "mempool.space"
AMBOSS = "api.amboss.space"
COINGECKO = "api.coingecko.com"

# Filled by _load(); written verbatim into _sources.json so every derived number
# is traceable to a hashed file.
SOURCES: list[dict] = []


def _load(source: str, endpoint: str):
    """Load a verified snapshot and remember its provenance record."""
    payload, record = latest_snapshot(source, endpoint)
    SOURCES.append(
        {
            "source": record["source"],
            "endpoint": record["endpoint"],
            "url": record["url"],
            "fetched_at_utc": record["fetched_at_utc"],
            "file": record["file"],
            "sha256": record["sha256"],
        }
    )
    return payload, record


def _require(value, what: str):
    """Refuse to build a table around a missing input."""
    if value is None:
        raise FetchError(
            f"{what} is absent from the snapshot. The build stops here rather "
            "than substituting a default."
        )
    return value


def _ts_to_iso(seconds) -> str:
    return datetime.fromtimestamp(int(seconds), tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _date_only(value: str) -> str:
    return str(value)[:10]


def _normalise_iso(value: str) -> str:
    """Bring a source's own timestamp onto the repository's UTC format.

    mempool.space stamps `added` with milliseconds; everything else in the
    manifest is second-resolution. One column must not mix the two.
    """
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# inputs
# --------------------------------------------------------------------------

def load_network(cfg: dict) -> dict:
    """mempool.space headline Lightning statistics."""
    payload, record = _load(MEMPOOL, "lightning_statistics_latest")
    latest = _require(
        payload.get("latest") if isinstance(payload, dict) else None,
        "lightning/statistics/latest -> latest",
    )
    for field in ("channel_count", "node_count", "total_capacity", "avg_capacity",
                  "med_capacity", "avg_fee_rate", "med_fee_rate",
                  "avg_base_fee_mtokens", "med_base_fee_mtokens"):
        _require(latest.get(field), f"lightning/statistics/latest -> {field}")

    return {
        "fetched_at_utc": record["fetched_at_utc"],
        # `added` is the timestamp mempool.space assigns to the statistics row
        # itself. It lags the fetch, and the paper must cite this date, not the
        # moment the script happened to run.
        "data_as_of_utc": _normalise_iso(latest["added"]),
        **latest,
    }


def load_history(cfg: dict) -> pd.DataFrame:
    interval = str(cfg["sources"]["mempool"]["history_interval"])
    payload, record = _load(MEMPOOL, f"lightning_statistics_{interval}")
    if not isinstance(payload, list) or not payload:
        raise FetchError(f"lightning/statistics/{interval} returned no series")

    frame = pd.DataFrame(payload)
    frame["date_utc"] = frame["added"].map(_ts_to_iso)
    frame["total_capacity_btc"] = frame["total_capacity"] / SAT_PER_BTC
    frame["avg_channel_capacity_sat"] = frame["total_capacity"] / frame["channel_count"]
    frame["interval"] = interval
    frame["snapshot_fetched_at_utc"] = record["fetched_at_utc"]
    return frame.sort_values("added").reset_index(drop=True)


def load_fee_rate(cfg: dict) -> dict:
    """sat/vB used to price channel opens and closes."""
    channel = cfg["model"]["channel"]
    endpoint = str(channel["fee_source"])
    tier = str(channel["fee_tier"])
    if endpoint not in ("fees_recommended", "fees_precise"):
        raise FetchError(
            f"model.channel.fee_source={endpoint!r} is not one of "
            "fees_recommended | fees_precise"
        )

    payload, record = _load(MEMPOOL, endpoint)
    if not isinstance(payload, dict) or tier not in payload:
        raise FetchError(
            f"{endpoint} has no tier {tier!r}; available: "
            f"{sorted(payload) if isinstance(payload, dict) else type(payload)}"
        )
    return {
        "sat_per_vb": float(payload[tier]),
        "tier": tier,
        "endpoint": endpoint,
        "all_tiers": payload,
        "fetched_at_utc": record["fetched_at_utc"],
    }


def load_price(cfg: dict) -> dict:
    coin = str(cfg["sources"]["coingecko"]["coin_id"])
    payload, record = _load(COINGECKO, "simple_price")
    quotes = _require(
        payload.get(coin) if isinstance(payload, dict) else None,
        f"CoinGecko simple/price -> {coin}",
    )
    usd = _require(quotes.get("usd"), "CoinGecko BTC/USD")
    return {
        "btc_usd": float(usd),
        "quotes": quotes,
        "fetched_at_utc": record["fetched_at_utc"],
    }


# --------------------------------------------------------------------------
# Amboss: outbound fee-rate distribution
# --------------------------------------------------------------------------

def _histogram_percentile(buckets: list[dict], q: float):
    """Percentile of a binned distribution, interpolated inside its bucket.

    `buckets` are (min, max, weight) in ascending order. Returns
    (value, exact_flag). exact_flag is False when the percentile falls in the
    open-ended top bucket, where no interpolation is defensible: the caller
    reports a lower bound instead of inventing a number.
    """
    total = sum(b["weight"] for b in buckets)
    if total <= 0:
        raise FetchError("fee-rate histogram is empty; cannot take percentiles")

    target = q * total
    cumulative = 0.0
    for bucket in buckets:
        if bucket["weight"] <= 0:
            continue
        if cumulative + bucket["weight"] >= target:
            if not math.isfinite(bucket["max"]):
                return bucket["min"], False
            share = (target - cumulative) / bucket["weight"]
            return bucket["min"] + share * (bucket["max"] - bucket["min"]), True
        cumulative += bucket["weight"]

    last = buckets[-1]
    return (last["max"] if math.isfinite(last["max"]) else last["min"]), math.isfinite(last["max"])


def load_fee_distribution(cfg: dict) -> dict:
    settings = cfg["fee_distribution"]
    side = str(settings["side"])
    weight_by = str(settings["weight_by"])
    if side not in ("local", "remote"):
        raise FetchError(f"fee_distribution.side={side!r} must be local or remote")
    if weight_by not in ("channels", "capacity"):
        raise FetchError(
            f"fee_distribution.weight_by={weight_by!r} must be channels or capacity"
        )

    payload, record = _load(AMBOSS, "node_fee_buckets")
    responses = _require(payload.get("responses"), "node_fee_buckets -> responses")

    key = f"{side}_buckets"
    weight_field = "amount_channels" if weight_by == "channels" else "total_capacity"

    merged: dict[int, dict] = {}
    nodes_used = 0
    nodes_skipped = []
    for pubkey, response in responses.items():
        node = ((response.get("data") or {}).get("getNode") or {}) if isinstance(response, dict) else {}
        graph_info = node.get("graph_info") or {}
        buckets = (graph_info.get("fee_buckets") or {}).get(key)
        if not buckets:
            nodes_skipped.append(pubkey)
            continue
        nodes_used += 1
        for bucket in buckets:
            index = int(bucket["index"])
            slot = merged.setdefault(
                index,
                {
                    "index": index,
                    "label": bucket["bucket_label"],
                    "min": float(bucket["min_limit"]),
                    "max": float(bucket["max_limit"]),
                    "weight": 0.0,
                },
            )
            slot["weight"] += float(bucket[weight_field])

    if not nodes_used:
        raise FetchError(
            "no node in the snapshot carries fee_buckets; refusing to publish a "
            "ppm distribution"
        )

    ordered = [merged[i] for i in sorted(merged)]
    total_weight = sum(b["weight"] for b in ordered)

    percentiles = {}
    for p in settings["percentiles"]:
        value, exact = _histogram_percentile(ordered, float(p) / 100.0)
        percentiles[int(p)] = {"value": value, "exact": exact}

    return {
        "buckets": ordered,
        "total_weight": total_weight,
        "percentiles": percentiles,
        "nodes_used": nodes_used,
        "nodes_skipped": nodes_skipped,
        "side": side,
        "weight_by": weight_by,
        "fetched_at_utc": record["fetched_at_utc"],
    }


# --------------------------------------------------------------------------
# Amboss: Magma liquidity market
# --------------------------------------------------------------------------

def load_magma_offers(cfg: dict) -> dict:
    """Annualise the ask side of the market: live open channel-lease offers.

    Magma prices a lease as base_fee + size * fee_rate / 1e6 for a term of
    min_block_length blocks. That term rate is annualised on the same
    52,560-block year Amboss uses for its own LNR benchmark, so offer-side and
    trade-side rates in table 2 are directly comparable.
    """
    magma = cfg["magma"]
    blocks_per_year = float(cfg["model"]["blocks_per_year"])
    reference_size = float(magma["reference_channel_size_sat"])
    statuses = set(magma["offer_status_filter"])
    sides = set(magma["offer_side_filter"])
    types = set(magma["offer_type_filter"])

    payload, record = _load(AMBOSS, "magma_offers")
    offers = _require(
        ((payload.get("data") or {}).get("getOffers") or {}).get("list"),
        "getOffers -> list",
    )

    rates = []
    rows = []
    for offer in offers:
        if offer.get("status") not in statuses:
            continue
        if offer.get("side") not in sides:
            continue
        if offer.get("offer_type") not in types:
            continue
        blocks = offer.get("min_block_length")
        if not blocks:
            continue

        min_size = float(offer["min_size"])
        max_size = float(offer["max_size"])
        size = min(max(reference_size, min_size), max_size)
        cost_sat = float(offer["base_fee"]) + size * float(offer["fee_rate"]) / 1e6
        term_rate = cost_sat / size
        annual = term_rate * blocks_per_year / float(blocks)

        rates.append(annual)
        rows.append(
            {
                "offer_id": offer.get("id"),
                "priced_size_sat": size,
                "term_blocks": int(blocks),
                "base_fee_sat": float(offer["base_fee"]),
                "fee_rate_ppm": float(offer["fee_rate"]),
                "lease_cost_sat": cost_sat,
                "term_rate": term_rate,
                "annual_rate": annual,
            }
        )

    if not rates:
        raise FetchError(
            "no Magma offer survived the config filters; table 2 would have no "
            "ask-side row"
        )

    return {
        "rates": sorted(rates),
        "rows": rows,
        "n": len(rates),
        "reference_size_sat": reference_size,
        "fetched_at_utc": record["fetched_at_utc"],
    }


def load_magma_orders(cfg: dict) -> dict:
    """The trade side: leases that actually executed.

    `lnr` is Amboss's Lightning Network Rate — the cost of the channel expressed
    over a one-year term of ~52,560 blocks — so it is already annualised and is
    used as returned, without re-scaling by block_duration.
    """
    magma = cfg["magma"]
    statuses = set(magma["order_status_filter"])
    min_size = magma.get("orders_min_size_sat")
    max_size = magma.get("orders_max_size_sat")

    payload, record = _load(AMBOSS, "magma_order_details")
    orders = _require(
        ((payload.get("data") or {}).get("getMarketMetrics") or {}).get("order_details"),
        "getMarketMetrics.order_details",
    )

    rates = []
    kept = []
    for order in orders:
        if order.get("status") not in statuses:
            continue
        if order.get("lnr") is None:
            continue
        size = float(order["size"])
        if min_size is not None and size < float(min_size):
            continue
        if max_size is not None and size > float(max_size):
            continue
        rates.append(float(order["lnr"]))
        kept.append(order)

    if not rates:
        raise FetchError(
            "no executed Magma lease survived the config filters; table 2 would "
            "have no trade-side row"
        )

    dates = [str(o["date"]) for o in kept]
    return {
        "rates": sorted(rates),
        "n": len(rates),
        "window_from": min(dates)[:10],
        "window_to": max(dates)[:10],
        "statuses": sorted(statuses),
        "fetched_at_utc": record["fetched_at_utc"],
    }


def load_lnr_index(cfg: dict) -> dict:
    """Amboss's published aggregate LNR series; its newest point is the index."""
    payload, record = _load(AMBOSS, "magma_lnr_series")
    series = _require(
        ((payload.get("data") or {}).get("getMarketMetrics") or {}).get("lnr_series"),
        "getMarketMetrics.lnr_series",
    )
    points = [p for p in series if p.get("lnr") is not None]
    if not points:
        raise FetchError("lnr_series carries no usable point")
    newest = max(points, key=lambda p: str(p["date"]))
    return {
        "date": str(newest["date"])[:10],
        "lnr": float(newest["lnr"]),
        "period": str(cfg["sources"]["amboss"]["lnr_period"]),
        "n_points": len(points),
        "fetched_at_utc": record["fetched_at_utc"],
    }


# --------------------------------------------------------------------------
# the model
# --------------------------------------------------------------------------

class Model:
    """Every term of APY_routing, in satoshis, from config + snapshot inputs."""

    def __init__(self, cfg: dict, network: dict, fees: dict, price: dict):
        model = cfg["model"]
        channel = model["channel"]

        self.days_per_year = float(model["days_per_year"])
        self.r_reb = float(model["rebalancing_share_of_earned"])
        self.sat_per_vb = float(fees["sat_per_vb"])
        self.open_vbytes = float(channel["open_vbytes"])
        self.close_vbytes = float(channel["close_vbytes"])
        self.lifetime_months = float(channel["lifetime_months"])
        if self.lifetime_months <= 0:
            raise FetchError("model.channel.lifetime_months must be > 0")

        # OBSERVED unless the author overrides it with an explicit assumption.
        override = channel.get("size_btc")
        if override is None:
            self.channel_size_sat = float(network["med_capacity"])
            self.channel_size_basis = "mempool.space med_capacity (observed)"
        else:
            self.channel_size_sat = float(override) * SAT_PER_BTC
            self.channel_size_basis = "config model.channel.size_btc (assumption)"
        if self.channel_size_sat <= 0:
            raise FetchError("channel size resolved to 0; cannot count channels")

        self.btc_usd = float(price["btc_usd"])
        self.opex_usd_per_year = float(model["opex_usd_per_year"])
        self.opex_sat = self.opex_usd_per_year / self.btc_usd * SAT_PER_BTC

        # The observed per-channel cost. onchain_amortized_sat() recomputes it
        # whenever a stress level is passed in.
        self.onchain_per_channel_sat = (
            (self.open_vbytes + self.close_vbytes) * self.sat_per_vb
        )

    # -- terms ------------------------------------------------------------

    def channels(self, capacity_btc: float) -> float:
        """Fractional by design: a node's capacity rarely divides evenly, and
        rounding up would overstate on-chain cost for the smallest scenario."""
        return capacity_btc * SAT_PER_BTC / self.channel_size_sat

    def onchain_amortized_sat(
        self, capacity_btc: float, sat_per_vb: float | None = None
    ) -> float:
        """`sat_per_vb` defaults to the observed tier; pass a level to stress it."""
        rate = self.sat_per_vb if sat_per_vb is None else float(sat_per_vb)
        per_channel = (self.open_vbytes + self.close_vbytes) * rate
        annualisation = MONTHS_PER_YEAR / self.lifetime_months
        return self.channels(capacity_btc) * per_channel * annualisation

    def f_earned_sat(self, capacity_btc: float, ppm: float, u: float) -> float:
        volume_sat = u * capacity_btc * SAT_PER_BTC * self.days_per_year
        return ppm * 1e-6 * volume_sat

    def terms(
        self,
        capacity_btc: float,
        ppm: float,
        u: float,
        sat_per_vb: float | None = None,
    ) -> dict:
        earned = self.f_earned_sat(capacity_btc, ppm, u)
        rebalancing = self.r_reb * earned
        onchain = self.onchain_amortized_sat(capacity_btc, sat_per_vb)
        opex = self.opex_sat
        net = earned - rebalancing - onchain - opex
        locked_sat = capacity_btc * SAT_PER_BTC
        return {
            "node_capacity_btc": capacity_btc,
            "ppm": ppm,
            "utilization": u,
            "f_earned_sat": earned,
            "f_rebalancing_sat": rebalancing,
            "onchain_amortized_sat": onchain,
            "opex_sat": opex,
            "net_income_sat": net,
            "apy_routing_pct": net / locked_sat * 100.0,
        }

    def apy_pct(
        self,
        capacity_btc: float,
        ppm: float,
        u: float,
        sat_per_vb: float | None = None,
    ) -> float:
        return self.terms(capacity_btc, ppm, u, sat_per_vb)["apy_routing_pct"]

    def break_even_u(
        self,
        capacity_btc: float,
        ppm: float,
        coc_pct: float,
        sat_per_vb: float | None = None,
    ):
        """Utilisation at which APY_routing meets the cost of capital.

        Closed form: APY is affine in u. Returns None when no u satisfies it
        (ppm or the rebalancing share leaves the slope at or below zero).
        """
        slope = ppm * 1e-6 * self.days_per_year * (1.0 - self.r_reb)
        if slope <= 0:
            return None
        locked_sat = capacity_btc * SAT_PER_BTC
        fixed = (
            self.onchain_amortized_sat(capacity_btc, sat_per_vb) + self.opex_sat
        ) / locked_sat
        return (coc_pct / 100.0 + fixed) / slope


# --------------------------------------------------------------------------
# tables
# --------------------------------------------------------------------------

def write_network_snapshot(
    network: dict, fees: dict, price: dict, history: pd.DataFrame
) -> pd.DataFrame:
    """Headline figures, each stamped with the as-of date of its OWN source.

    The three sources in this table were observed at different moments and must
    not share one date. The mempool.space statistics row carries `added`, the
    date mempool.space assigned to the row, which lags collection by up to a
    fortnight. The fee tiers are a live estimate with no timestamp of their own,
    so they carry the moment they were fetched. The CoinGecko quote carries the
    exchange's own `last_updated_at`, which is what the price actually refers to.
    """
    stats_as_of = str(network["data_as_of_utc"])
    stats_source = "mempool.space lightning/statistics/latest"
    stats_fetched = network["fetched_at_utc"]

    fee_as_of = fees["fetched_at_utc"]
    fee_source = f"mempool.space {fees['endpoint']}"

    # CoinGecko returns last_updated_at only when asked for it; fall back to the
    # fetch time rather than borrowing a date from another source.
    last_updated = price["quotes"].get("last_updated_at")
    price_as_of = _ts_to_iso(last_updated) if last_updated else price["fetched_at_utc"]
    price_source = "CoinGecko simple/price"

    rows = [
        ("total_capacity_btc", network["total_capacity"] / SAT_PER_BTC, "BTC", stats_source, stats_as_of, stats_fetched),
        ("total_capacity_sat", network["total_capacity"], "sat", stats_source, stats_as_of, stats_fetched),
        ("node_count", network["node_count"], "nodes", stats_source, stats_as_of, stats_fetched),
        ("channel_count", network["channel_count"], "channels", stats_source, stats_as_of, stats_fetched),
        ("avg_channel_capacity_sat", network["avg_capacity"], "sat", stats_source, stats_as_of, stats_fetched),
        ("med_channel_capacity_sat", network["med_capacity"], "sat", stats_source, stats_as_of, stats_fetched),
        ("avg_fee_rate_ppm", network["avg_fee_rate"], "ppm", stats_source, stats_as_of, stats_fetched),
        ("med_fee_rate_ppm", network["med_fee_rate"], "ppm", stats_source, stats_as_of, stats_fetched),
        ("avg_base_fee_msat", network["avg_base_fee_mtokens"], "msat", stats_source, stats_as_of, stats_fetched),
        ("med_base_fee_msat", network["med_base_fee_mtokens"], "msat", stats_source, stats_as_of, stats_fetched),
        ("tor_nodes", network["tor_nodes"], "nodes", stats_source, stats_as_of, stats_fetched),
        ("clearnet_nodes", network["clearnet_nodes"], "nodes", stats_source, stats_as_of, stats_fetched),
        (f"onchain_fee_{fees['tier']}_sat_per_vb", fees["sat_per_vb"], "sat/vB", fee_source, fee_as_of, fee_as_of),
        ("btc_usd", price["btc_usd"], "USD", price_source, price_as_of, price["fetched_at_utc"]),
    ]
    for currency, value in price["quotes"].items():
        if currency in ("usd", "last_updated_at"):
            continue
        rows.append(
            (f"btc_{currency}", value, currency.upper(), price_source,
             price_as_of, price["fetched_at_utc"])
        )

    frame = pd.DataFrame(
        rows,
        columns=[
            "metric",
            "value",
            "unit",
            "source",
            "data_as_of_utc",
            "snapshot_fetched_at_utc",
        ],
    )
    frame.to_csv(DERIVED_DIR / "network_snapshot.csv", index=False)

    history.to_csv(DERIVED_DIR / "lightning_history.csv", index=False)
    return frame


def write_ppm_distribution(distribution: dict, network: dict) -> pd.DataFrame:
    rows = []
    requested = distribution["nodes_used"] + len(distribution["nodes_skipped"])
    basis = (
        f"Amboss {distribution['side']}_buckets over {distribution['nodes_used']} "
        f"of {requested} ranked nodes, weighted by {distribution['weight_by']} "
        f"(n={distribution['total_weight']:.0f}), interpolated within the "
        "containing bucket"
    )
    for p, result in sorted(distribution["percentiles"].items()):
        rows.append(
            {
                "metric": f"p{p}_fee_rate_ppm",
                "value": round(result["value"], 2),
                "exact": result["exact"],
                "basis": basis,
                "snapshot_date": _date_only(distribution["fetched_at_utc"]),
                "note": (
                    ""
                    if result["exact"]
                    else "falls in the open-ended top bucket; value is a lower bound"
                ),
            }
        )

    # The network-wide comparison. mempool.space computes these over every
    # announced channel, so they are a wider but coarser view than the
    # top-node histogram above; the paper should quote both.
    rows.append(
        {
            "metric": "nodes_in_distribution",
            "value": distribution["nodes_used"],
            "exact": True,
            "basis": basis,
            "snapshot_date": _date_only(distribution["fetched_at_utc"]),
            "note": (
                f"{len(distribution['nodes_skipped'])} of {requested} ranked nodes "
                f"returned an empty {distribution['side']}_buckets array and "
                "contribute nothing; their pubkeys are listed in _sources.json."
            ),
        }
    )
    rows.append(
        {
            "metric": "channels_in_distribution",
            "value": round(distribution["total_weight"], 0),
            "exact": True,
            "basis": basis,
            "snapshot_date": _date_only(distribution["fetched_at_utc"]),
            "note": f"total histogram weight, weighted by {distribution['weight_by']}",
        }
    )
    rows.append(
        {
            "metric": "network_med_fee_rate_ppm",
            "value": network["med_fee_rate"],
            "exact": True,
            "basis": "mempool.space lightning/statistics/latest med_fee_rate (all announced channels)",
            "snapshot_date": _date_only(network["data_as_of_utc"]),
            "note": "",
        }
    )
    rows.append(
        {
            "metric": "network_avg_fee_rate_ppm",
            "value": network["avg_fee_rate"],
            "exact": True,
            "basis": "mempool.space lightning/statistics/latest avg_fee_rate (all announced channels)",
            "snapshot_date": _date_only(network["data_as_of_utc"]),
            "note": "",
        }
    )

    frame = pd.DataFrame(rows)
    frame.to_csv(DERIVED_DIR / "ppm_distribution.csv", index=False)

    buckets = pd.DataFrame(
        [
            {
                "index": b["index"],
                "bucket_label": b["label"],
                "min_ppm": b["min"],
                "max_ppm": b["max"],
                "weight": b["weight"],
                "share": b["weight"] / distribution["total_weight"],
            }
            for b in distribution["buckets"]
        ]
    )
    buckets.to_csv(DERIVED_DIR / "ppm_histogram.csv", index=False)
    return frame


def write_cost_of_capital(cfg: dict, offers: dict, orders: dict, index: dict) -> pd.DataFrame:
    rows = []

    offer_basis = (
        f"Magma open SELL offers, n={offers['n']}, priced at a "
        f"{offers['reference_size_sat']:,.0f} sat reference channel "
        "(clamped into each offer's own min/max size) as "
        "base_fee + size*fee_rate/1e6 over min_block_length, annualised on a "
        "52,560-block year"
    )
    offer_date = _date_only(offers["fetched_at_utc"])
    for label, value in _quartiles(offers["rates"]).items():
        rows.append(
            {
                "source": f"magma_offers_{label}",
                "observed_rate_annual_pct": round(value * 100.0, 4),
                "basis": offer_basis,
                "snapshot_date": offer_date,
                "note": (
                    "ask side: what sellers quote, not what buyers paid. Excludes "
                    "the Amboss platform fee (amboss_fee_rate), whose unit is not "
                    "documented, so this understates the buyer's all-in cost."
                ),
            }
        )

    order_basis = (
        f"executed Magma leases, status {'/'.join(orders['statuses'])}, "
        f"{orders['window_from']}..{orders['window_to']}, n={orders['n']}, "
        "Amboss LNR as returned (already annualised over ~52,560 blocks)"
    )
    order_date = _date_only(orders["fetched_at_utc"])
    for label, value in _quartiles(orders["rates"]).items():
        rows.append(
            {
                "source": f"magma_orders_{label}",
                "observed_rate_annual_pct": round(value * 100.0, 4),
                "basis": order_basis,
                "snapshot_date": order_date,
                "note": "trade side: rates on leases that actually executed.",
            }
        )

    rows.append(
        {
            "source": "amboss_lnr_index_latest",
            "observed_rate_annual_pct": round(index["lnr"] * 100.0, 4),
            "basis": (
                f"Amboss LNR aggregate series, period {index['period']}, newest "
                f"point {index['date']} of {index['n_points']}"
            ),
            "snapshot_date": _date_only(index["fetched_at_utc"]),
            "note": "Amboss's own published benchmark; reported for comparison.",
        }
    )

    # The opportunity cost of self-funded BTC is what those coins would earn
    # leased out on Magma instead of being routed, so it is read off one of the
    # observed rows above rather than asserted. Built last because it depends on
    # them; inserted first because it is the paper's baseline comparison.
    rows.insert(0, _own_btc_row(cfg, rows))

    frame = pd.DataFrame(
        rows,
        columns=["source", "observed_rate_annual_pct", "basis", "snapshot_date", "note"],
    )
    frame.to_csv(DERIVED_DIR / "table2_cost_of_capital.csv", index=False)
    return frame


def _own_btc_row(cfg: dict, observed_rows: list[dict]) -> dict:
    """The `own_btc_opportunity` row: observed by default, overridable."""
    settings = cfg["cost_of_capital"]["own_btc_opportunity"]
    override = settings.get("override_annual_pct")

    if override is not None:
        return {
            "source": "own_btc_opportunity",
            "observed_rate_annual_pct": float(override),
            "basis": "config cost_of_capital.own_btc_opportunity.override_annual_pct",
            "snapshot_date": "",
            "note": (
                "ASSUMPTION, not observed: an explicit override of the return "
                "forgone on self-funded BTC."
            ),
        }

    wanted = str(settings["from_source"])
    by_source = {row["source"]: row for row in observed_rows}
    if wanted not in by_source:
        raise FetchError(
            f"cost_of_capital.own_btc_opportunity.from_source={wanted!r} is not "
            f"an observed row. Available: {sorted(by_source)}"
        )
    source_row = by_source[wanted]
    return {
        "source": "own_btc_opportunity",
        "observed_rate_annual_pct": source_row["observed_rate_annual_pct"],
        "basis": f"OBSERVED, taken from {wanted}: {source_row['basis']}",
        "snapshot_date": source_row["snapshot_date"],
        "note": (
            "the return forgone on self-funded BTC, measured as what the same "
            f"coins would earn leased out on Magma ({wanted}). Set "
            "cost_of_capital.own_btc_opportunity.override_annual_pct to assert a "
            "different figure."
        ),
    }


def _quartiles(sorted_rates: list[float]) -> dict:
    return {
        "p25": _percentile(sorted_rates, 25),
        "median": statistics.median(sorted_rates),
        "p75": _percentile(sorted_rates, 75),
        "p90": _percentile(sorted_rates, 90),
    }


def _percentile(sorted_values: list[float], p: float) -> float:
    """Linear-interpolation percentile on an already-sorted list."""
    if not sorted_values:
        raise FetchError("percentile of an empty sample")
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = (len(sorted_values) - 1) * p / 100.0
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return sorted_values[low]
    return sorted_values[low] + (sorted_values[high] - sorted_values[low]) * (position - low)


def resolve_cost_of_capital(cfg: dict, table2: pd.DataFrame) -> tuple[str, float]:
    wanted = str(cfg["cost_of_capital"]["reference_source"])
    match = table2.loc[table2["source"] == wanted, "observed_rate_annual_pct"]
    if match.empty:
        raise FetchError(
            f"cost_of_capital.reference_source={wanted!r} is not a row of "
            f"table2_cost_of_capital.csv. Available: {list(table2['source'])}"
        )
    return wanted, float(match.iloc[0])


def scenario_grid(cfg: dict) -> list[tuple[float, float]]:
    """Every (capacity, ppm) pair the scenario tables are evaluated on."""
    scenarios = cfg["model"]["scenarios"]
    capacities = [float(c) for c in scenarios["capacities_btc"]]
    ppm_list = [float(p) for p in scenarios["ppm_list"]]
    if not capacities or not ppm_list:
        raise FetchError(
            "model.scenarios needs at least one capacity and one ppm value"
        )
    return [(capacity, ppm) for capacity in capacities for ppm in ppm_list]


def write_scenarios(cfg: dict, model: Model, coc_pct: float) -> pd.DataFrame:
    """One row per (capacity, ppm) pair, at the baseline utilisation.

    Break-even utilisation is carried alongside because it depends on capacity:
    OPEX is a fixed USD cost, so it weighs far more heavily on a small node.
    """
    u = float(cfg["model"]["baseline"]["utilization"])

    rows = []
    for capacity, ppm in scenario_grid(cfg):
        terms = model.terms(capacity, ppm, u)
        break_even = model.break_even_u(capacity, ppm, coc_pct)
        terms["cost_of_capital_annual_pct"] = round(coc_pct, 4)
        terms["break_even_u"] = "" if break_even is None else round(break_even, 6)
        terms["break_even_attainable"] = (
            "" if break_even is None else bool(0.0 <= break_even <= 1.0)
        )
        rows.append(terms)

    frame = pd.DataFrame(rows)[
        [
            "node_capacity_btc",
            "ppm",
            "utilization",
            "f_earned_sat",
            "f_rebalancing_sat",
            "onchain_amortized_sat",
            "opex_sat",
            "net_income_sat",
            "apy_routing_pct",
            "cost_of_capital_annual_pct",
            "break_even_u",
            "break_even_attainable",
        ]
    ]
    for column in frame.columns:
        if column.endswith("_sat"):
            frame[column] = frame[column].round(0)
    frame["apy_routing_pct"] = frame["apy_routing_pct"].round(4)
    frame.to_csv(DERIVED_DIR / "table3_scenarios.csv", index=False)
    return frame


def write_onchain_sensitivity(
    cfg: dict, model: Model, fees: dict, coc_pct: float
) -> pd.DataFrame:
    """The scenario grid re-run at several on-chain fee levels.

    The snapshot caught a near-empty mempool, where every recommended tier reads
    1 sat/vB and on-chain amortisation all but vanishes. That is a real
    observation, not a representative one, so the same grid is also evaluated at
    the configured stress levels. The observed row is the base case and is
    labelled `observed`; every other row is labelled `stress` and is an
    assumption about a fee market that was not seen in this snapshot.
    """
    observed = float(model.sat_per_vb)
    levels = [(observed, "observed")]
    for level in cfg["onchain_sensitivity"]["sat_per_vb_levels"]:
        level = float(level)
        # Skip a configured level that coincides with the observation rather
        # than emitting the same number twice under two different labels.
        if abs(level - observed) < 1e-9:
            continue
        levels.append((level, "stress"))

    u = float(cfg["model"]["baseline"]["utilization"])
    rows = []
    for sat_per_vb, kind in levels:
        for capacity, ppm in scenario_grid(cfg):
            terms = model.terms(capacity, ppm, u, sat_per_vb)
            break_even = model.break_even_u(capacity, ppm, coc_pct, sat_per_vb)
            rows.append(
                {
                    "sat_per_vb": sat_per_vb,
                    "basis": kind,
                    "source": (
                        f"mempool.space {fees['endpoint']}.{fees['tier']}"
                        if kind == "observed"
                        else "config onchain_sensitivity.sat_per_vb_levels"
                    ),
                    "node_capacity_btc": capacity,
                    "ppm": ppm,
                    "utilization": u,
                    "onchain_amortized_sat": round(terms["onchain_amortized_sat"], 0),
                    "net_income_sat": round(terms["net_income_sat"], 0),
                    "apy_routing_pct": round(terms["apy_routing_pct"], 4),
                    "cost_of_capital_annual_pct": round(coc_pct, 4),
                    "break_even_u": "" if break_even is None else round(break_even, 6),
                    "break_even_attainable": (
                        "" if break_even is None else bool(0.0 <= break_even <= 1.0)
                    ),
                }
            )

    frame = pd.DataFrame(rows)
    frame.to_csv(DERIVED_DIR / "onchain_sensitivity.csv", index=False)
    return frame


def utilization_grid(cfg: dict) -> list[float]:
    sensitivity = cfg["sensitivity"]
    start = float(sensitivity["utilization_min"])
    stop = float(sensitivity["utilization_max"])
    step = float(sensitivity["utilization_step"])
    if step <= 0:
        raise FetchError("sensitivity.utilization_step must be > 0")

    grid = []
    steps = int(round((stop - start) / step))
    for i in range(steps + 1):
        grid.append(round(start + i * step, 10))
    return grid


def write_sensitivity(cfg: dict, model: Model, coc_source: str, coc_pct: float) -> pd.DataFrame:
    capacity = float(cfg["model"]["baseline"]["capacity_btc"])
    grid = utilization_grid(cfg)

    rows = []
    for ppm in cfg["sensitivity"]["ppm_list"]:
        ppm = float(ppm)
        break_even = model.break_even_u(capacity, ppm, coc_pct)
        if break_even is not None:
            # The closed form and the model must agree, or one of them is wrong.
            achieved = model.apy_pct(capacity, ppm, break_even)
            if abs(achieved - coc_pct) > 1e-6:
                raise FetchError(
                    f"break-even solution disagrees with the model at {ppm} ppm: "
                    f"APY(u*={break_even}) = {achieved}% but the cost of capital "
                    f"is {coc_pct}%"
                )
        for u in grid:
            terms = model.terms(capacity, ppm, u)
            rows.append(
                {
                    "node_capacity_btc": capacity,
                    "ppm": ppm,
                    "utilization": u,
                    "f_earned_sat": round(terms["f_earned_sat"], 0),
                    "net_income_sat": round(terms["net_income_sat"], 0),
                    "apy_routing_pct": round(terms["apy_routing_pct"], 4),
                    "cost_of_capital_annual_pct": round(coc_pct, 4),
                    "cost_of_capital_source": coc_source,
                    "break_even_u": (
                        "" if break_even is None else round(break_even, 6)
                    ),
                    "break_even_attainable": (
                        "" if break_even is None else bool(0.0 <= break_even <= 1.0)
                    ),
                }
            )

    frame = pd.DataFrame(rows)
    frame.to_csv(DERIVED_DIR / "sensitivity.csv", index=False)
    return frame


# --------------------------------------------------------------------------
# figure
# --------------------------------------------------------------------------

CM_PER_INCH = 2.54
# Four line styles and four markers, so the curves separate without colour.
LINE_STYLES = [
    ("solid", "o"),
    ((0, (6, 2)), "s"),
    ((0, (1, 1.5)), "^"),
    ((0, (7, 2, 1, 2)), "D"),
]


def write_figure(cfg: dict, model: Model, coc_source: str, coc_pct: float) -> None:
    figure_cfg = cfg["figure"]
    capacity = float(cfg["model"]["baseline"]["capacity_btc"])
    grid = utilization_grid(cfg)
    ppm_list = [float(p) for p in figure_cfg["ppm_curves"]]

    plt.rcParams.update(
        {
            "font.family": figure_cfg["font_family"],
            "font.size": float(figure_cfg["base_font_pt"]),
            "axes.labelsize": float(figure_cfg["base_font_pt"]),
            "axes.titlesize": float(figure_cfg["base_font_pt"]),
            "xtick.labelsize": float(figure_cfg["base_font_pt"]),
            "ytick.labelsize": float(figure_cfg["base_font_pt"]),
            "legend.fontsize": float(figure_cfg["base_font_pt"]),
            "axes.linewidth": 0.6,
        }
    )

    width_in = float(figure_cfg["width_cm"]) / CM_PER_INCH
    height_in = float(figure_cfg["height_cm"]) / CM_PER_INCH
    fig, ax = plt.subplots(figsize=(width_in, height_in))

    plot_rows = []
    # Handles are collected explicitly: matplotlib orders the legend by draw
    # order, which would drop the break-even marker in between two curves.
    curve_handles = []
    star_handle = None
    for i, ppm in enumerate(ppm_list):
        style, marker = LINE_STYLES[i % len(LINE_STYLES)]
        values = [model.apy_pct(capacity, ppm, u) for u in grid]
        break_even = model.break_even_u(capacity, ppm, coc_pct)

        # Every break-even point sits on the same horizontal line, so inline
        # labels collide with each other and with the curves. The value goes in
        # the legend instead; the axis keeps only the marker.
        if break_even is None:
            label = f"{ppm:.0f} ppm ($u^*$ undefined)"
        elif grid[0] <= break_even <= grid[-1]:
            label = f"{ppm:.0f} ppm ($u^*$ = {break_even:.2f})"
        else:
            label = f"{ppm:.0f} ppm ($u^*$ = {break_even:.2f}, off scale)"

        line, = ax.plot(
            grid,
            values,
            linestyle=style,
            marker=marker,
            markersize=3.2,
            markevery=2,
            linewidth=1.0,
            color="black",
            label=label,
        )
        curve_handles.append(line)
        for u, value in zip(grid, values):
            plot_rows.append(
                {
                    "ppm": ppm,
                    "utilization": u,
                    "apy_routing_pct": round(value, 6),
                    "cost_of_capital_annual_pct": round(coc_pct, 6),
                    "break_even_u": "" if break_even is None else round(break_even, 6),
                }
            )

        # Mark break-even only where it is actually on the plotted axis;
        # a marker at u > 1 would suggest a point that does not exist.
        if break_even is not None and grid[0] <= break_even <= grid[-1]:
            star, = ax.plot(
                [break_even],
                [coc_pct],
                marker="*",
                markersize=9,
                color="black",
                linestyle="none",
                zorder=5,
                label="break-even $u^*$",
            )
            if star_handle is None:
                star_handle = star

    coc_handle = ax.axhline(
        coc_pct,
        linestyle=(0, (3, 3)),
        linewidth=0.9,
        color="0.35",
        label=f"cost of capital ({coc_pct:.2f}% / yr)",
    )
    ax.axhline(0.0, linewidth=0.6, color="0.75", zorder=0)

    ax.set_xlabel("Utilisation $u$ (daily routed volume / locked capital)")
    ax.set_ylabel("Routing APY (% per year)")
    ax.set_xlim(grid[0], grid[-1])
    ax.grid(True, linewidth=0.3, color="0.85")
    ax.set_axisbelow(True)
    handles = curve_handles + ([star_handle] if star_handle else []) + [coc_handle]
    legend = ax.legend(
        handles=handles,
        loc="upper left",
        frameon=True,
        framealpha=1.0,
        edgecolor="0.6",
    )
    legend.get_frame().set_linewidth(0.5)

    # tight_layout, not bbox_inches="tight": the journal asks for a specific
    # printed width, and a tight bounding box would silently shrink it.
    fig.tight_layout(pad=0.4)

    FIGURES_DIR.mkdir(parents=True, exist_ok=True)
    png_path = FIGURES_DIR / "fig1_apy_vs_utilization.png"
    fig.savefig(png_path, dpi=int(figure_cfg["dpi"]))
    plt.close(fig)

    frame = pd.DataFrame(plot_rows)
    frame["node_capacity_btc"] = capacity
    frame["cost_of_capital_source"] = coc_source
    frame.to_csv(FIGURES_DIR / "fig1_apy_vs_utilization.csv", index=False)
    print(f"  wrote {png_path.relative_to(REPO_ROOT)} ({figure_cfg['dpi']} dpi)")


# --------------------------------------------------------------------------

def main() -> int:
    cfg = load_config()
    DERIVED_DIR.mkdir(parents=True, exist_ok=True)

    print("[build_tables] loading verified snapshots")
    network = load_network(cfg)
    history = load_history(cfg)
    fees = load_fee_rate(cfg)
    price = load_price(cfg)
    distribution = load_fee_distribution(cfg)
    offers = load_magma_offers(cfg)
    orders = load_magma_orders(cfg)
    index = load_lnr_index(cfg)

    model = Model(cfg, network, fees, price)

    print("[build_tables] writing derived tables")
    write_network_snapshot(network, fees, price, history)
    write_ppm_distribution(distribution, network)
    table2 = write_cost_of_capital(cfg, offers, orders, index)
    coc_source, coc_pct = resolve_cost_of_capital(cfg, table2)
    table3 = write_scenarios(cfg, model, coc_pct)
    onchain = write_onchain_sensitivity(cfg, model, fees, coc_pct)
    sensitivity = write_sensitivity(cfg, model, coc_source, coc_pct)

    print("[build_tables] writing figure")
    write_figure(cfg, model, coc_source, coc_pct)

    provenance = {
        "built_at_utc": iso_utc(utc_now()),
        "config": str((REPO_ROOT / "config.yaml").relative_to(REPO_ROOT)),
        "cost_of_capital_source": coc_source,
        "cost_of_capital_annual_pct": coc_pct,
        "channel_size_sat": model.channel_size_sat,
        "channel_size_basis": model.channel_size_basis,
        "onchain_fee_sat_per_vb": model.sat_per_vb,
        "onchain_fee_endpoint": fees["endpoint"],
        "onchain_fee_tier": fees["tier"],
        "onchain_stress_levels_sat_per_vb": [
            float(level) for level in cfg["onchain_sensitivity"]["sat_per_vb_levels"]
        ],
        "scenario_ppm_list": [float(x) for x in cfg["model"]["scenarios"]["ppm_list"]],
        "btc_usd": model.btc_usd,
        "fee_distribution": {
            "side": distribution["side"],
            "weight_by": distribution["weight_by"],
            "nodes_used": distribution["nodes_used"],
            "nodes_skipped": distribution["nodes_skipped"],
            "total_weight": distribution["total_weight"],
        },
        "inputs": SOURCES,
    }
    (DERIVED_DIR / "_sources.json").write_text(
        json.dumps(provenance, indent=2) + "\n", encoding="utf-8"
    )

    print()
    print(f"  network       : {network['channel_count']:,} channels, "
          f"{network['node_count']:,} nodes, "
          f"{network['total_capacity'] / SAT_PER_BTC:,.1f} BTC "
          f"(as of {network['data_as_of_utc'][:10]})")
    print(f"  channel size  : {model.channel_size_sat:,.0f} sat "
          f"[{model.channel_size_basis}]")
    print(f"  on-chain fee  : {model.sat_per_vb} sat/vB "
          f"[{fees['endpoint']}.{fees['tier']}]")
    print(f"  BTC/USD       : {model.btc_usd:,.0f}")
    print(f"  cost of capital: {coc_pct:.2f}%/yr [{coc_source}]")
    print(f"  ppm p50       : {distribution['percentiles'][50]['value']:.0f} "
          f"(top-{distribution['nodes_used']} nodes) vs "
          f"{network['med_fee_rate']} network-wide")
    print()
    print("  scenarios (u = %.2f, %s sat/vB observed):"
          % (float(cfg["model"]["baseline"]["utilization"]), model.sat_per_vb))
    summary = table3[
        ["node_capacity_btc", "ppm", "apy_routing_pct", "break_even_u",
         "break_even_attainable"]
    ]
    print(summary.to_string(index=False))

    print()
    print("  on-chain stress (APY %, same grid):")
    pivot = onchain.pivot_table(
        index=["node_capacity_btc", "ppm"],
        columns="sat_per_vb",
        values="apy_routing_pct",
    )
    print(pivot.to_string())

    print()
    for ppm in sorted(sensitivity["ppm"].unique()):
        subset = sensitivity[sensitivity["ppm"] == ppm]
        value = subset["break_even_u"].iloc[0]
        attainable = subset["break_even_attainable"].iloc[0]
        flag = "" if attainable in (True, "True") else "  (u > 1: unreachable)"
        print(f"  break-even u @ {ppm:>6.0f} ppm : {value}{flag}"
              f"   [1 BTC, sensitivity grid]")

    print("\n[build_tables] done")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except FetchError as exc:
        print(f"[build_tables] FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
