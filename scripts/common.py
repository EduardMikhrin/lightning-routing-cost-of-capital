"""Shared plumbing for the snapshot scripts.

Three rules are enforced here rather than in each script:

1. A non-200 response is a hard failure. No stubs, no cached fallbacks, no
   "reasonable defaults". A missing number must stay missing.
2. A raw file is written once and never rewritten. Re-running produces a new
   timestamped file next to the old one.
3. Every write appends a manifest line carrying the SHA-256 of the bytes on
   disk, so a later build can prove the file was not edited by hand.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = REPO_ROOT / "data" / "raw"
DERIVED_DIR = REPO_ROOT / "data" / "derived"
FIGURES_DIR = REPO_ROOT / "figures"
MANIFEST_PATH = RAW_DIR / "manifest.jsonl"
CONFIG_PATH = REPO_ROOT / "config.yaml"


class FetchError(RuntimeError):
    """Raised when a source cannot be collected. Always fatal by design."""


# --------------------------------------------------------------------------
# config & time
# --------------------------------------------------------------------------

def load_config(path: Path = CONFIG_PATH) -> dict:
    if not path.exists():
        raise FetchError(f"config not found: {path}")
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(dt: datetime | None = None) -> str:
    """ISO 8601 UTC, e.g. 2026-09-13T12:34:56Z."""
    return (dt or utc_now()).strftime("%Y-%m-%dT%H:%M:%SZ")


def file_stamp(dt: datetime | None = None) -> str:
    """Filesystem-safe UTC stamp, e.g. 2026-09-13T12-34-56Z."""
    return (dt or utc_now()).strftime("%Y-%m-%dT%H-%M-%SZ")


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def _sleep(seconds: float) -> None:
    if seconds > 0:
        time.sleep(seconds)


def _request(
    method: str,
    url: str,
    cfg: dict,
    *,
    headers: dict | None = None,
    json_body: dict | None = None,
) -> requests.Response:
    """One request with bounded exponential-backoff retries.

    Retries only what retrying can fix: transport errors, 429 and 5xx. Any
    other non-200 is reported immediately with the body attached, because a
    400 or 404 means the request itself is wrong and a retry just hides it.
    """
    http = cfg["http"]
    retries = int(http["retries"])
    backoff_base = float(http["backoff_base_seconds"])
    timeout = float(http["timeout_seconds"])

    request_headers = {"User-Agent": http["user_agent"], "Accept": "application/json"}
    if headers:
        request_headers.update(headers)

    last_error = ""
    for attempt in range(1, retries + 1):
        try:
            response = requests.request(
                method,
                url,
                headers=request_headers,
                json=json_body,
                timeout=timeout,
            )
        except requests.RequestException as exc:
            last_error = f"transport error: {exc}"
        else:
            if response.status_code == 200:
                return response
            body = response.text[:500].replace("\n", " ")
            last_error = f"HTTP {response.status_code}: {body}"
            retryable = response.status_code == 429 or response.status_code >= 500
            if not retryable:
                raise FetchError(f"{method} {url} failed, not retryable -> {last_error}")

        if attempt < retries:
            delay = backoff_base ** attempt
            print(
                f"  attempt {attempt}/{retries} failed ({last_error}); "
                f"retrying in {delay:.1f}s",
                file=sys.stderr,
            )
            _sleep(delay)

    raise FetchError(f"{method} {url} failed after {retries} attempts -> {last_error}")


def get_json(url: str, cfg: dict, *, headers: dict | None = None) -> tuple[Any, bytes, int]:
    """GET returning (parsed, raw_bytes, status). Raw bytes are stored verbatim."""
    response = _request("GET", url, cfg, headers=headers)
    raw = response.content
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise FetchError(f"GET {url} returned non-JSON body: {exc}") from exc
    _sleep(float(cfg["http"]["pause_between_requests_seconds"]))
    return parsed, raw, response.status_code


def post_graphql(
    endpoint: str,
    query: str,
    cfg: dict,
    *,
    variables: dict | None = None,
    api_key: str | None = None,
    allow_graphql_errors: bool = False,
) -> tuple[Any, bytes, int]:
    """POST a GraphQL document.

    A GraphQL server answers 200 with an `errors` array, so a transport-level
    success is not enough: treat any `errors` payload as a fatal failure too.

    `allow_graphql_errors` is for per-item sweeps where one bad item (a node
    that has left the graph, say) must not discard the other 99 results. The
    caller then owns the error and must record it in the snapshot.
    """
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    body: dict[str, Any] = {"query": query}
    if variables:
        body["variables"] = variables

    response = _request("POST", endpoint, cfg, headers=headers, json_body=body)
    raw = response.content
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise FetchError(f"GraphQL {endpoint} returned non-JSON body: {exc}") from exc

    if not allow_graphql_errors and isinstance(parsed, dict) and parsed.get("errors"):
        raise FetchError(
            f"GraphQL {endpoint} returned errors: {json.dumps(parsed['errors'])[:800]}"
        )
    _sleep(float(cfg["http"]["pause_between_requests_seconds"]))
    return parsed, raw, response.status_code


# --------------------------------------------------------------------------
# raw storage & manifest
# --------------------------------------------------------------------------

def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def save_raw(
    source: str,
    endpoint: str,
    payload: bytes,
    *,
    url: str,
    http_status: int,
    fetched_at: datetime,
    extra: dict | None = None,
) -> Path:
    """Write raw bytes unmodified and append the manifest record.

    Refuses to touch an existing path: raw snapshots are append-only history.
    """
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{source}_{endpoint}_{file_stamp(fetched_at)}.json"
    path = RAW_DIR / filename

    if path.exists():
        raise FetchError(
            f"refusing to overwrite existing raw snapshot: {path}. "
            "Raw files are immutable; wait a second and re-run."
        )

    # Exclusive create: also loses safely to a concurrent writer.
    with open(path, "xb") as fh:
        fh.write(payload)

    record = {
        "source": source,
        "url": url,
        "fetched_at_utc": iso_utc(fetched_at),
        "http_status": http_status,
        "file": str(path.relative_to(REPO_ROOT)),
        "sha256": sha256_bytes(payload),
        "endpoint": endpoint,
        "bytes": len(payload),
    }
    if extra:
        record.update(extra)

    with MANIFEST_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    print(f"  saved {record['file']}  ({len(payload)} B, sha256 {record['sha256'][:12]}…)")
    return path


# --------------------------------------------------------------------------
# reading snapshots back (used by build_tables.py)
# --------------------------------------------------------------------------

def read_manifest() -> list[dict]:
    if not MANIFEST_PATH.exists():
        raise FetchError(
            f"no manifest at {MANIFEST_PATH}. Run the fetch scripts first "
            "(make fetch)."
        )
    records = []
    with MANIFEST_PATH.open("r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise FetchError(f"manifest line {lineno} is not valid JSON: {exc}") from exc
    return records


def latest_snapshot(source: str, endpoint: str) -> tuple[Any, dict]:
    """Load the newest snapshot for (source, endpoint), verifying its hash.

    Returns (parsed_json, manifest_record). Raises if the endpoint was never
    collected, if the file is gone, or if its bytes no longer match the hash
    recorded at fetch time — a hand-edited snapshot must not reach a table.
    """
    candidates = [
        rec for rec in read_manifest()
        if rec.get("source") == source and rec.get("endpoint") == endpoint
    ]
    if not candidates:
        raise FetchError(
            f"no snapshot recorded for source={source} endpoint={endpoint}. "
            "Run the matching fetch script."
        )

    record = max(candidates, key=lambda rec: rec["fetched_at_utc"])
    path = REPO_ROOT / record["file"]
    if not path.exists():
        raise FetchError(f"manifest references a missing file: {path}")

    payload = path.read_bytes()
    actual = sha256_bytes(payload)
    if actual != record["sha256"]:
        raise FetchError(
            f"checksum mismatch for {path}\n"
            f"  manifest: {record['sha256']}\n"
            f"  on disk : {actual}\n"
            "The raw snapshot was modified after collection. Refusing to build."
        )
    return json.loads(payload), record


def load_api_key(var_name: str) -> str | None:
    """Read a key from the environment, loading .env if python-dotenv is present."""
    try:
        from dotenv import load_dotenv

        load_dotenv(REPO_ROOT / ".env")
    except ImportError:
        pass
    value = os.environ.get(var_name, "").strip()
    return value or None
