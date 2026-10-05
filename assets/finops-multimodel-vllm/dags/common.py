"""
Shared helpers for the FinOps multi-model DAGs.

The DAGs drive the FinOps simulation against the LiteLLM gateway:
  * finops_setup     — create per-team virtual keys + budgets in LiteLLM.
  * generate_traffic — fire synthetic multi-model / multi-team chat (real token spend).
  * backfill_history — write BACKDATED synthetic spend into Prometheus (remote-write).

Config (env, injected by the apache-airflow component):
  LITELLM_BASE_URL       http://litellm:4000
  LITELLM_MASTER_KEY     sk-guardrails-demo   (proxy master key; admin on the API)
  PROM_REMOTE_WRITE_URL  http://prometheus-server:80/api/v1/write
  TRAFFIC_REQUESTS       40                   (requests per generate_traffic run)
  BACKFILL_DAYS          14                   (days of history for backfill_history)

The DAGs use only `requests` (already present in the Airflow image) plus the Python
standard library — the Prometheus remote-write client below is implemented from
scratch (protobuf + snappy), so NO custom Airflow image and NO extra pip deps are
needed. The stock application-collection apache-airflow image works as-is.
"""
from __future__ import annotations

import os
import struct
import time

import requests

LITELLM_BASE_URL = os.environ.get("LITELLM_BASE_URL", "http://litellm:4000").rstrip("/")
LITELLM_MASTER_KEY = os.environ.get("LITELLM_MASTER_KEY", "sk-guardrails-demo")
PROM_REMOTE_WRITE_URL = os.environ.get(
    "PROM_REMOTE_WRITE_URL", "http://prometheus-server:80/api/v1/write"
)
TRAFFIC_REQUESTS = int(os.environ.get("TRAFFIC_REQUESTS", "40"))
BACKFILL_DAYS = int(os.environ.get("BACKFILL_DAYS", "14"))
HTTP_TIMEOUT = 120

# The three models served by LiteLLM (must match the model_name entries in the
# proxy_config model_list) and their per-1k-token prices (USD). Kept here so the
# traffic + backfill DAGs agree on model mix and pricing.
MODELS = [
    # model_name,   input_$/1k, output_$/1k, popularity weight
    ("qwen-0.5b",   0.00005,    0.0001,      0.50),
    ("qwen-1.5b",   0.0002,     0.0004,      0.32),
    ("qwen-3b",     0.0006,     0.0012,      0.18),
]

# Per-team virtual keys (fixed key values → deterministic, no state to pass between
# DAGs) and their monthly budgets (USD). use_case is attached as a request tag for
# chargeback attribution.
TEAMS = [
    # team_alias,    virtual key,          monthly budget $, use_case,      traffic weight
    ("engineering",  "sk-team-eng",        50.0,             "code-assist", 0.40),
    ("data-science", "sk-team-ds",         80.0,             "analysis",    0.25),
    ("marketing",    "sk-team-mkt",        30.0,             "copywriting", 0.20),
    ("support",      "sk-team-support",    20.0,             "helpdesk",    0.15),
]


# --------------------------------------------------------------------------- #
# LiteLLM admin API (uses the master key)
# --------------------------------------------------------------------------- #
def _admin_headers() -> dict:
    return {
        "Authorization": f"Bearer {LITELLM_MASTER_KEY}",
        "Content-Type": "application/json",
    }


def litellm_post(path: str, body: dict, ok_conflict: bool = False) -> dict:
    """POST to the LiteLLM admin API. When ok_conflict, swallow 400/409 (already
    exists) so setup DAGs are idempotent. On other errors, raise WITH the response
    body so LiteLLM's actual message (e.g. 'admin only route', 'DB not connected')
    shows up in the Airflow logs instead of a bare HTTPError."""
    r = requests.post(
        f"{LITELLM_BASE_URL}{path}", json=body, headers=_admin_headers(), timeout=HTTP_TIMEOUT
    )
    if ok_conflict and r.status_code in (400, 409):
        return {"status": "exists", "detail": r.text[:300]}
    if not r.ok:
        raise RuntimeError(f"LiteLLM POST {path} -> HTTP {r.status_code}: {r.text[:600]}")
    return r.json() if r.content else {}


def wait_for_litellm(retries: int = 30, delay: int = 10) -> None:
    """Block until the proxy answers /health/liveliness (models may still warm)."""
    last = None
    for _ in range(retries):
        try:
            r = requests.get(f"{LITELLM_BASE_URL}/health/liveliness", timeout=10)
            if r.ok:
                return
            last = r.status_code
        except Exception as e:  # noqa: BLE001
            last = e
        time.sleep(delay)
    raise RuntimeError(f"LiteLLM not ready at {LITELLM_BASE_URL} (last={last})")


# --------------------------------------------------------------------------- #
# Prometheus remote-write (backdated samples for instant history)
#
# Implemented from scratch so the DAGs need no extra pip deps (the stock Airflow
# image has no pip). Remote-write is a snappy-compressed protobuf WriteRequest:
#   WriteRequest { repeated TimeSeries timeseries = 1; }
#   TimeSeries   { repeated Label labels = 1; repeated Sample samples = 2; }
#   Label        { string name = 1; string value = 2; }
#   Sample       { double value = 1; int64 timestamp = 2; }
# We encode the protobuf by hand and wrap it in a snappy BLOCK made entirely of
# literals (a valid, if uncompressed, snappy block) — no snappy C library needed.
# --------------------------------------------------------------------------- #
def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _ld(field: int, data: bytes) -> bytes:
    """Length-delimited field (wire type 2): tag + varint(len) + data."""
    return _varint((field << 3) | 2) + _varint(len(data)) + data


def _encode_write_request(samples: list[dict]) -> bytes:
    body = bytearray()
    for s in samples:
        labels = bytearray()
        for name, value in s["metric"].items():
            lbl = _ld(1, str(name).encode()) + _ld(2, str(value).encode())
            labels += _ld(1, lbl)  # TimeSeries.labels
        # Sample: value (field 1, 64-bit double) + timestamp (field 2, varint).
        sample = (_varint((1 << 3) | 1) + struct.pack("<d", float(s["value"]))
                  + _varint((2 << 3) | 0) + _varint(int(s["timestamp"])))
        ts = bytes(labels) + _ld(2, sample)  # TimeSeries.samples
        body += _ld(1, ts)  # WriteRequest.timeseries
    return bytes(body)


def _snappy_block(data: bytes) -> bytes:
    """Minimal snappy block: preamble length + literal runs only (<=60 bytes each)."""
    out = bytearray(_varint(len(data)))
    for i in range(0, len(data), 60):
        chunk = data[i:i + 60]
        out.append((len(chunk) - 1) << 2)  # literal tag, length-1 in the top 6 bits
        out += chunk
    return bytes(out)


def remote_write(samples: list[dict]) -> None:
    """Push samples to Prometheus via the remote-write receiver.

    Each sample: {"metric": {"__name__": ..., <labels>}, "value": float, "timestamp": ms}.
    """
    if not samples:
        return
    batch = 500
    for i in range(0, len(samples), batch):
        payload = _snappy_block(_encode_write_request(samples[i:i + batch]))
        r = requests.post(
            PROM_REMOTE_WRITE_URL,
            data=payload,
            headers={
                "Content-Encoding": "snappy",
                "Content-Type": "application/x-protobuf",
                "X-Prometheus-Remote-Write-Version": "0.1.0",
            },
            timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
