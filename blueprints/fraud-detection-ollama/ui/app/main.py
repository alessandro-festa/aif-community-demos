"""
Fraud / AML investigator dashboard (SUSE) — backend.

Reads the Airflow-produced tables in PostgreSQL (accounts, transactions, fraud_cases,
account_scores, flagged_accounts, model_metrics) and the Milvus account-embedding index,
and uses a locally-served LLM (vLLM or Ollama, OpenAI-compatible) as an AML analyst that
explains flagged cases.

Inspired by / with thanks to SantanderAI/gen-fraud-graph (Apache-2.0) and
srinivas-gajulaa/genai-fraud-detection (analyst-explanation pattern).

The SAME code drives both variants — only env differs:
  * Ollama : OPENAI_BASE_URL=http://localhost:11434/v1  LLM_MODEL=qwen2.5:1.5b
  * vLLM   : OPENAI_BASE_URL=http://localhost:8000/v1   LLM_MODEL=Qwen/Qwen2.5-3B-Instruct

Config (env):
  POSTGRES_URI     postgresql://fraud:fraud@localhost:5432/fraud
  MILVUS_URI       http://localhost:19530
  OPENAI_BASE_URL  http://localhost:11434/v1
  LLM_MODEL        qwen2.5:1.5b
  OPENAI_API_KEY   EMPTY
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import psycopg2
import psycopg2.extras
import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

POSTGRES_URI = os.environ.get("POSTGRES_URI", "postgresql://fraud:fraud@localhost:5432/fraud")
MILVUS_URI = os.environ.get("MILVUS_URI", "http://localhost:19530").rstrip("/")
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "http://localhost:11434/v1").rstrip("/")
LLM_MODEL = os.environ.get("LLM_MODEL", "qwen2.5:1.5b")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "EMPTY")
HTTP_TIMEOUT = 120
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

app = FastAPI(title="Fraud / AML Investigator (SUSE)")

FEATURES = ["out_degree", "in_degree", "out_amount", "in_amount", "mean_amount",
            "max_amount", "high_value_edges", "in_cycle", "balance", "risk_score"]


# --------------------------------------------------------------------------- #
# Postgres helpers
# --------------------------------------------------------------------------- #
def pg(sql: str, params=None) -> list[dict]:
    conn = psycopg2.connect(POSTGRES_URI)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params or ())
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def pg_ok() -> bool:
    try:
        psycopg2.connect(POSTGRES_URI).close()
        return True
    except Exception:
        return False


def table_exists(name: str) -> bool:
    try:
        return bool(pg("SELECT to_regclass(%s) AS t", (name,))[0]["t"])
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# LLM (OpenAI-compatible; works for vLLM and Ollama)
# --------------------------------------------------------------------------- #
ANALYST_SYSTEM = (
    "You are an anti-money-laundering (AML) analyst. Given a bank account's behavioural "
    "features, model scores and any laundering-ring context, assess the money-laundering / "
    "financial-crime risk. Consider typologies such as cyclic layering, structuring, "
    "smurfing and fan-in/fan-out. Respond with ONLY a compact JSON object: "
    '{"typology": "<short label>", "risk_rationale": "<2-3 sentences>", '
    '"recommended_action": "<short>", "confidence": 0.0-1.0}.'
)


def llm_explain(context: str) -> dict:
    payload = {
        "model": LLM_MODEL,
        "temperature": 0.2,
        "max_tokens": 400,
        "messages": [
            {"role": "system", "content": ANALYST_SYSTEM},
            {"role": "user", "content": context},
        ],
    }
    r = requests.post(f"{OPENAI_BASE_URL}/chat/completions", json=payload,
                      headers={"Authorization": f"Bearer {OPENAI_API_KEY}"}, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return parse_json(r.json()["choices"][0]["message"]["content"])


def parse_json(text: str) -> dict:
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
    return {"typology": "unknown", "risk_rationale": text[:300],
            "recommended_action": "review manually", "confidence": 0.0}


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
@app.get("/api/health")
def health():
    llm = False
    try:
        requests.get(f"{OPENAI_BASE_URL}/models", timeout=10).raise_for_status()
        llm = True
    except Exception:
        pass
    milvus = False
    try:
        requests.post(f"{MILVUS_URI}/v2/vectordb/collections/list", json={}, timeout=10).raise_for_status()
        milvus = True
    except Exception:
        pass
    return {"postgres": pg_ok(), "milvus": milvus, "llm": llm,
            "model": LLM_MODEL, "ready": table_exists("flagged_accounts")}


@app.get("/api/overview")
def overview():
    if not table_exists("accounts"):
        raise HTTPException(409, "No dataset yet — run the generate_fraud_dataset DAG.")
    stats = {
        "accounts": pg("SELECT count(*) c FROM accounts")[0]["c"],
        "transactions": pg("SELECT count(*) c FROM transactions")[0]["c"],
        "rings": pg("SELECT count(*) c FROM fraud_cases")[0]["c"] if table_exists("fraud_cases") else 0,
        "flagged": pg("SELECT count(*) c FROM flagged_accounts")[0]["c"] if table_exists("flagged_accounts") else 0,
    }
    metrics = pg("SELECT * FROM model_metrics LIMIT 1") if table_exists("model_metrics") else []
    return {"stats": stats, "metrics": metrics[0] if metrics else None}


@app.get("/api/flagged")
def flagged():
    if not table_exists("flagged_accounts"):
        raise HTTPException(409, "No flagged accounts yet — run engineer_and_train + flag_and_anomaly.")
    return {"flagged": pg(
        "SELECT account_id, xgb_score, anomaly_score, in_ring, rank "
        "FROM flagged_accounts ORDER BY rank ASC LIMIT 50")}


@app.get("/api/account/{account_id}")
def account(account_id: str):
    acc = pg("SELECT * FROM accounts WHERE account_id=%s", (account_id,))
    if not acc:
        raise HTTPException(404, "account not found")
    feats = pg("SELECT * FROM account_scores WHERE account_id=%s", (account_id,))
    txs = pg("SELECT tx_id, src_id, dst_id, amount, timestamp, description, is_fraud_edge "
             "FROM transactions WHERE src_id=%s OR dst_id=%s ORDER BY amount DESC LIMIT 25",
             (account_id, account_id))
    return {"account": acc[0], "features": feats[0] if feats else None, "transactions": txs}


class ExplainReq(BaseModel):
    account_id: str


@app.post("/api/explain")
def explain(req: ExplainReq):
    feats = pg("SELECT * FROM account_scores WHERE account_id=%s", (req.account_id,))
    if not feats:
        raise HTTPException(404, "account not scored")
    f = feats[0]
    flag = pg("SELECT * FROM flagged_accounts WHERE account_id=%s", (req.account_id,))
    fl = flag[0] if flag else {}
    context = (
        f"Account {req.account_id}\n"
        f"Model fraud score (XGBoost): {fl.get('xgb_score', f.get('xgb_score'))}\n"
        f"Anomaly score: {fl.get('anomaly_score', 'n/a')}\n"
        f"In laundering ring (high-value cycle): {'yes' if f.get('in_cycle') else 'no'}\n"
        "Behavioural features: "
        + ", ".join(f"{k}={f.get(k)}" for k in FEATURES) + "\n"
        "Assess money-laundering / financial-crime risk."
    )
    try:
        verdict = llm_explain(context)
    except Exception as e:
        raise HTTPException(502, f"LLM unreachable at {OPENAI_BASE_URL}: {e}")
    return {"account_id": req.account_id, "verdict": verdict}


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/", StaticFiles(directory=STATIC_DIR), name="static")
