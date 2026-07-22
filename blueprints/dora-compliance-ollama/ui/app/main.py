"""
DORA Compliance Analysis dashboard (SUSE) — backend.

Reads the Airflow-produced DORA tables in PostgreSQL (incidents, incidents_classified,
mart_bafin_report, mart_vendor_risk, mart_sla_breach) and a Milvus incident index, and uses a
locally-served LLM (Ollama or vLLM, OpenAI-compatible) in two ways:

  * /api/explain — a per-incident DORA compliance-analyst verdict (why this severity, which
    authority + deadline, recommended reporting action) — the "explain what happened" pattern.
  * /api/agent — a tool-calling COMPLIANCE AGENT: an OpenAI function-calling loop whose tools
    search the data through Airflow's Postgres connection, run Milvus semantic search, read the
    compliance marts, and trigger/monitor the pipeline via the Airflow REST API. The response
    includes the agent's tool-call trace so you can see it "acting like an agent".

The SAME code drives both variants — only env differs:
  * Ollama : OPENAI_BASE_URL=http://localhost:11434/v1  LLM_MODEL=qwen2.5:1.5b
  * vLLM   : OPENAI_BASE_URL=http://localhost:8000/v1   LLM_MODEL=Qwen/Qwen2.5-3B-Instruct

Config (env):
  POSTGRES_URI     postgresql://dora:dora@localhost:5432/dora
  MILVUS_URI       http://localhost:19530
  OPENAI_BASE_URL  http://localhost:11434/v1
  LLM_MODEL        qwen2.5:1.5b
  OPENAI_API_KEY   EMPTY
  EMBED_DIM        256
  AIRFLOW_BASE_URL http://localhost:8080   (Airflow 3 REST API + /auth/token)
  AIRFLOW_USER     admin
  AIRFLOW_PASSWORD admin
"""
from __future__ import annotations

import hashlib
import inspect
import json
import math
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

POSTGRES_URI = os.environ.get("POSTGRES_URI", "postgresql://dora:dora@localhost:5432/dora")
MILVUS_URI = os.environ.get("MILVUS_URI", "http://localhost:19530").rstrip("/")
MILVUS_TOKEN = os.environ.get("MILVUS_TOKEN", "")
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "http://localhost:11434/v1").rstrip("/")
LLM_MODEL = os.environ.get("LLM_MODEL", "qwen2.5:1.5b")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "EMPTY")
EMBED_DIM = int(os.environ.get("EMBED_DIM", "256"))
INCIDENTS_COLLECTION = os.environ.get("INCIDENTS_COLLECTION", "incidents")
AIRFLOW_BASE_URL = os.environ.get("AIRFLOW_BASE_URL", "http://localhost:8080").rstrip("/")
AIRFLOW_USER = os.environ.get("AIRFLOW_USER", "admin")
AIRFLOW_PASSWORD = os.environ.get("AIRFLOW_PASSWORD", "admin")
HTTP_TIMEOUT = 120
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# The pipeline DAGs, in run order — used by the agent's trigger_pipeline tool.
PIPELINE_DAGS = ["simulate_incidents", "classify_and_load", "build_marts",
                 "index_incidents", "check_compliance_alerts"]

app = FastAPI(title="DORA Compliance Analysis (SUSE)")


# --------------------------------------------------------------------------- #
# PostgreSQL helpers
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
# Deterministic hashing embedding — MUST match dags/common.hash_embed exactly.
# --------------------------------------------------------------------------- #
def hash_embed(text: str, dim: int = EMBED_DIM) -> list[float]:
    vec = [0.0] * dim
    for tok in re.findall(r"[a-z0-9]+", (text or "").lower()):
        h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
        vec[h % dim] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def _milvus_post(path: str, body: dict) -> dict:
    headers = {"Content-Type": "application/json"}
    if MILVUS_TOKEN:
        headers["Authorization"] = f"Bearer {MILVUS_TOKEN}"
    r = requests.post(f"{MILVUS_URI}{path}", json=body, headers=headers, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict) and data.get("code") not in (0, None):
        raise RuntimeError(f"Milvus error on {path}: {data}")
    return data


def milvus_ok() -> bool:
    try:
        _milvus_post("/v2/vectordb/collections/list", {})
        return True
    except Exception:
        return False


def milvus_search_incidents(query: str, top_k: int = 5) -> list[dict]:
    data = _milvus_post("/v2/vectordb/entities/search", {
        "collectionName": INCIDENTS_COLLECTION,
        "data": [hash_embed(query)],
        "limit": top_k,
        "outputFields": ["incident_id", "dora_severity", "incident_type", "text"],
        "searchParams": {"metricType": "COSINE"},
    })
    return data.get("data") or []


# --------------------------------------------------------------------------- #
# LLM (OpenAI-compatible; works for vLLM and Ollama)
# --------------------------------------------------------------------------- #
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
    return {}


def chat(messages: list[dict], tools: list[dict] | None = None, temperature: float = 0.2) -> dict:
    payload = {"model": LLM_MODEL, "temperature": temperature, "max_tokens": 800,
               "messages": messages}
    if tools:
        payload["tools"] = tools
    r = requests.post(f"{OPENAI_BASE_URL}/chat/completions", json=payload,
                      headers={"Authorization": f"Bearer {OPENAI_API_KEY}"}, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]


# --------------------------------------------------------------------------- #
# Airflow REST API (Airflow 3: POST /auth/token → JWT, then /api/v2)
# --------------------------------------------------------------------------- #
_af_token: dict = {"value": None}


def _airflow_token() -> str:
    if _af_token["value"]:
        return _af_token["value"]
    r = requests.post(f"{AIRFLOW_BASE_URL}/auth/token",
                      json={"username": AIRFLOW_USER, "password": AIRFLOW_PASSWORD},
                      timeout=30)
    r.raise_for_status()
    tok = r.json().get("access_token") or r.json().get("jwt_token", "")
    _af_token["value"] = tok
    return tok


def _airflow(method: str, path: str, body: dict | None = None) -> dict:
    def _call():
        headers = {"Authorization": f"Bearer {_airflow_token()}",
                   "Content-Type": "application/json"}
        return requests.request(method, f"{AIRFLOW_BASE_URL}/api/v2{path}",
                                headers=headers, json=body, timeout=HTTP_TIMEOUT)
    r = _call()
    if r.status_code == 401:  # token expired — refresh once
        _af_token["value"] = None
        r = _call()
    r.raise_for_status()
    return r.json() if r.text else {}


def airflow_trigger(dag_id: str) -> dict:
    return _airflow("POST", f"/dags/{dag_id}/dagRuns", {"logical_date": None})


def airflow_latest_run(dag_id: str) -> dict:
    data = _airflow("GET", f"/dags/{dag_id}/dagRuns?order_by=-run_after&limit=1")
    runs = data.get("dag_runs") or []
    if not runs:
        return {"dag_id": dag_id, "state": "no runs yet"}
    r = runs[0]
    return {"dag_id": dag_id, "state": r.get("state"), "run_id": r.get("dag_run_id"),
            "start_date": r.get("start_date"), "end_date": r.get("end_date")}


# --------------------------------------------------------------------------- #
# Agent tools — the executors behind the OpenAI function-calling schemas.
# All data reads go through the same Postgres connection Airflow writes to.
# --------------------------------------------------------------------------- #
def tool_search_incidents(severity: str = "", incident_type: str = "", provider: str = "",
                          limit: int = 10) -> dict:
    if not table_exists("incidents_classified"):
        return {"error": "no data yet — run the pipeline first"}
    where, params = [], []
    if severity:
        where.append("dora_severity = %s"); params.append(severity.lower())
    if incident_type:
        where.append("incident_type = %s"); params.append(incident_type)
    if provider:
        where.append("ict_third_party_provider ILIKE %s"); params.append(f"%{provider}%")
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    params.append(max(1, min(int(limit or 10), 25)))
    rows = pg(f"SELECT incident_id, dora_severity, incident_type, ict_third_party_provider, "
              f"clients_affected_pct, financial_impact_eur, detection_ts, deadline_ts, "
              f"classification_reason FROM incidents_classified {clause} "
              f"ORDER BY financial_impact_eur DESC LIMIT %s", tuple(params))
    return {"count": len(rows), "incidents": rows}


def tool_semantic_search(query: str, limit: int = 5) -> dict:
    if not milvus_ok():
        return {"error": "Milvus unreachable"}
    try:
        hits = milvus_search_incidents(query, max(1, min(int(limit or 5), 10)))
    except Exception as e:
        return {"error": f"semantic search failed: {e}"}
    return {"matches": [{"incident_id": h.get("incident_id"),
                         "severity": h.get("dora_severity"),
                         "type": h.get("incident_type"),
                         "score": round(float(h.get("distance", 0)), 4),
                         "text": (h.get("text") or "")[:300]} for h in hits]}


def tool_get_bafin_report(limit: int = 15) -> dict:
    if not table_exists("mart_bafin_report"):
        return {"error": "no BaFin report yet — run build_marts"}
    rows = pg("SELECT incident_id, institution_id, dora_severity, incident_type, "
              "ict_third_party_provider, deadline_hours, deadline_ts, reported "
              "FROM mart_bafin_report ORDER BY deadline_ts ASC LIMIT %s",
              (max(1, min(int(limit or 15), 50)),))
    return {"count": len(rows), "reportable": rows}


def tool_get_sla_breaches() -> dict:
    if not table_exists("mart_sla_breach"):
        return {"error": "no SLA-breach mart yet — run check_compliance_alerts"}
    rows = pg("SELECT incident_id, institution_id, dora_severity, deadline_ts, "
              "hours_remaining, status FROM mart_sla_breach "
              "WHERE status IN ('BREACHED','IMMINENT') ORDER BY hours_remaining ASC")
    return {"count": len(rows), "breaches": rows}


def tool_get_vendor_risk(limit: int = 10) -> dict:
    if not table_exists("mart_vendor_risk"):
        return {"error": "no vendor-risk mart yet — run build_marts"}
    rows = pg("SELECT provider, provider_tier, incidents, critical, major, "
              "total_impact_eur, max_clients_pct FROM mart_vendor_risk "
              "ORDER BY critical DESC, major DESC LIMIT %s",
              (max(1, min(int(limit or 10), 25)),))
    return {"count": len(rows), "vendors": rows}


def tool_trigger_pipeline(dag_id: str = "") -> dict:
    try:
        if not dag_id:
            triggered = [{"dag_id": d, "run": airflow_trigger(d).get("dag_run_id")}
                         for d in PIPELINE_DAGS]
            return {"triggered": triggered,
                    "note": "DAGs triggered; they run in dependency order — poll status."}
        if dag_id not in PIPELINE_DAGS:
            return {"error": f"unknown dag_id; choose one of {PIPELINE_DAGS} or omit for all"}
        run = airflow_trigger(dag_id)
        return {"dag_id": dag_id, "run_id": run.get("dag_run_id"), "state": run.get("state")}
    except Exception as e:
        return {"error": f"Airflow trigger failed: {e}"}


def tool_pipeline_status() -> dict:
    try:
        return {"status": [airflow_latest_run(d) for d in PIPELINE_DAGS]}
    except Exception as e:
        return {"error": f"Airflow status failed: {e}"}


TOOLS = [
    {"type": "function", "function": {
        "name": "search_incidents",
        "description": "Search classified ICT incidents in the DORA database. Filter by "
                       "severity (critical/major/minor), incident_type, and/or ICT provider.",
        "parameters": {"type": "object", "properties": {
            "severity": {"type": "string", "enum": ["critical", "major", "minor"]},
            "incident_type": {"type": "string"},
            "provider": {"type": "string"},
            "limit": {"type": "integer"}}}}},
    {"type": "function", "function": {
        "name": "semantic_search_incidents",
        "description": "Find incidents semantically similar to a free-text query (Milvus).",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "get_bafin_report",
        "description": "List reportable (CRITICAL/MAJOR) incidents and their BaFin deadlines.",
        "parameters": {"type": "object", "properties": {"limit": {"type": "integer"}}}}},
    {"type": "function", "function": {
        "name": "get_sla_breaches",
        "description": "List incidents whose BaFin reporting deadline is breached or imminent.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "get_vendor_risk",
        "description": "ICT third-party provider concentration risk: incidents + severity + "
                       "financial impact per provider.",
        "parameters": {"type": "object", "properties": {"limit": {"type": "integer"}}}}},
    {"type": "function", "function": {
        "name": "trigger_pipeline",
        "description": "Run the DORA pipeline via the Airflow REST API. Omit dag_id to run all "
                       "stages in order, or pass one of: " + ", ".join(PIPELINE_DAGS) + ".",
        "parameters": {"type": "object", "properties": {"dag_id": {"type": "string"}}}}},
    {"type": "function", "function": {
        "name": "pipeline_status",
        "description": "Latest Airflow run state for each pipeline DAG.",
        "parameters": {"type": "object", "properties": {}}}},
]

TOOL_IMPL = {
    "search_incidents": tool_search_incidents,
    "semantic_search_incidents": tool_semantic_search,
    "get_bafin_report": tool_get_bafin_report,
    "get_sla_breaches": tool_get_sla_breaches,
    "get_vendor_risk": tool_get_vendor_risk,
    "trigger_pipeline": tool_trigger_pipeline,
    "pipeline_status": tool_pipeline_status,
}

AGENT_SYSTEM = (
    "You are a DORA (EU Digital Operational Resilience Act) compliance agent for a financial "
    "institution. You help compliance officers understand ICT incidents and their BaFin "
    "Article 18 reporting obligations (CRITICAL → notify within 4h, MAJOR → 72h, MINOR → "
    "internal log only). Use the provided tools to look up real data before answering — never "
    "invent incident numbers, providers or deadlines. You may run or re-run the data pipeline "
    "via the tools when asked. Be concise, factual, and cite incident_ids and providers you "
    "found. When a reporting deadline is breached, say so plainly."
)

ANALYST_SYSTEM = (
    "You are a DORA / BaFin Article 18 compliance analyst. Given one ICT incident's details and "
    "its rule-based classification, explain the compliance position. Respond with ONLY a compact "
    "JSON object: {\"severity_justification\": \"<why this DORA severity, 1-2 sentences>\", "
    "\"authority_and_deadline\": \"<who to notify (BaFin) and by when>\", "
    "\"recommended_action\": \"<short next step>\", \"confidence\": 0.0-1.0}."
)


# --------------------------------------------------------------------------- #
# API — dashboard
# --------------------------------------------------------------------------- #
@app.get("/api/health")
def health():
    llm = False
    try:
        requests.get(f"{OPENAI_BASE_URL}/models", timeout=10).raise_for_status()
        llm = True
    except Exception:
        pass
    airflow = False
    try:
        r = requests.get(f"{AIRFLOW_BASE_URL}/api/v2/version", timeout=10)
        # Accept 200 (public version endpoint) or 401 (up, but auth-gated) — both mean the
        # Airflow API server is really there, not just some other listener on the port.
        airflow = r.status_code in (200, 401) and ("version" in r.text.lower() or r.status_code == 401)
    except Exception:
        pass
    return {"postgres": pg_ok(), "milvus": milvus_ok(), "llm": llm, "airflow": airflow,
            "model": LLM_MODEL, "ready": table_exists("incidents_classified")}


@app.get("/api/overview")
def overview():
    if not table_exists("incidents_classified"):
        raise HTTPException(409, "No classified incidents yet — run the pipeline DAGs.")
    sev = {r["dora_severity"]: r["c"] for r in pg(
        "SELECT dora_severity, count(*) c FROM incidents_classified GROUP BY dora_severity")}
    stats = {
        "incidents": pg("SELECT count(*) c FROM incidents_classified")[0]["c"],
        "critical": sev.get("critical", 0),
        "major": sev.get("major", 0),
        "minor": sev.get("minor", 0),
        "reportable": pg("SELECT count(*) c FROM mart_bafin_report")[0]["c"] if table_exists("mart_bafin_report") else 0,
    }
    breaches = {"BREACHED": 0, "IMMINENT": 0}
    if table_exists("mart_sla_breach"):
        for r in pg("SELECT status, count(*) c FROM mart_sla_breach GROUP BY status"):
            breaches[r["status"]] = r["c"]
    vendors = pg("SELECT provider, provider_tier, incidents, critical, major, total_impact_eur "
                 "FROM mart_vendor_risk ORDER BY critical DESC, major DESC LIMIT 6") \
        if table_exists("mart_vendor_risk") else []
    return {"stats": stats, "breaches": breaches, "vendors": vendors}


@app.get("/api/incidents")
def incidents(severity: str = "", limit: int = 50):
    if not table_exists("incidents_classified"):
        raise HTTPException(409, "No classified incidents yet — run the pipeline DAGs.")
    if severity:
        return {"incidents": pg(
            "SELECT incident_id, institution_id, dora_severity, incident_type, "
            "ict_third_party_provider, clients_affected_pct, financial_impact_eur, "
            "detection_ts, deadline_ts FROM incidents_classified WHERE dora_severity=%s "
            "ORDER BY financial_impact_eur DESC LIMIT %s", (severity.lower(), min(limit, 200)))}
    return {"incidents": pg(
        "SELECT incident_id, institution_id, dora_severity, incident_type, "
        "ict_third_party_provider, clients_affected_pct, financial_impact_eur, "
        "detection_ts, deadline_ts FROM incidents_classified "
        "ORDER BY (dora_severity='critical') DESC, (dora_severity='major') DESC, "
        "financial_impact_eur DESC LIMIT %s", (min(limit, 200),))}


@app.get("/api/incident/{incident_id}")
def incident(incident_id: str):
    rows = pg("SELECT * FROM incidents_classified WHERE incident_id=%s", (incident_id,))
    if not rows:
        raise HTTPException(404, "incident not found")
    return {"incident": rows[0]}


# --------------------------------------------------------------------------- #
# API — explain (per-incident compliance verdict)
# --------------------------------------------------------------------------- #
class ExplainReq(BaseModel):
    incident_id: str


@app.post("/api/explain")
def explain(req: ExplainReq):
    rows = pg("SELECT * FROM incidents_classified WHERE incident_id=%s", (req.incident_id,))
    if not rows:
        raise HTTPException(404, "incident not found")
    i = rows[0]
    context = (
        f"Incident {i['incident_id']} ({i['institution_type']} {i['institution_id']})\n"
        f"Type: {i['incident_type']}; affected systems: {i['affected_systems']}\n"
        f"Clients affected: {i['clients_affected_pct']}%; financial impact: €{i['financial_impact_eur']:,.0f}\n"
        f"ICT third-party provider: {i['ict_third_party_provider'] or 'none'} "
        f"(tier {i['provider_tier'] or 'n/a'}); cross-border: {i['is_cross_border']}\n"
        f"Rule-based classification: {i['dora_severity'].upper()} — {i['classification_reason']}\n"
        f"BaFin notification required: {i['bafin_notification_required']}; "
        f"deadline: {i['deadline_hours']}h (by {i['deadline_ts']})\n"
        "Explain the DORA/BaFin compliance position for this incident."
    )
    try:
        msg = chat([{"role": "system", "content": ANALYST_SYSTEM},
                    {"role": "user", "content": context}])
        verdict = parse_json(msg.get("content") or "")
    except Exception as e:
        raise HTTPException(502, f"LLM unreachable at {OPENAI_BASE_URL}: {e}")
    if not verdict:
        verdict = {"severity_justification": (msg.get("content") or "")[:400],
                   "authority_and_deadline": f"{i['deadline_hours']}h (BaFin)" if i["deadline_hours"] else "no report",
                   "recommended_action": "review manually", "confidence": 0.0}
    return {"incident_id": req.incident_id, "classification": i["dora_severity"], "verdict": verdict}


# --------------------------------------------------------------------------- #
# API — compliance agent (tool-calling loop)
# --------------------------------------------------------------------------- #
class AgentReq(BaseModel):
    message: str
    history: list[dict] = []


@app.post("/api/agent")
def agent(req: AgentReq):
    messages = [{"role": "system", "content": AGENT_SYSTEM},
                *[m for m in req.history if m.get("role") in ("user", "assistant")],
                {"role": "user", "content": req.message}]
    trace: list[dict] = []
    try:
        for _step in range(6):  # bounded agent loop
            msg = chat(messages, tools=TOOLS)
            tool_calls = msg.get("tool_calls") or []
            if not tool_calls:
                return {"reply": (msg.get("content") or "").strip() or
                        "I couldn't find anything relevant.", "trace": trace}
            # Record the assistant turn (with its tool calls) then execute each tool.
            messages.append({"role": "assistant", "content": msg.get("content") or "",
                             "tool_calls": tool_calls})
            for tc in tool_calls:
                fn = tc.get("function", {})
                name = fn.get("name", "")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except Exception:
                    args = {}
                impl = TOOL_IMPL.get(name)
                if impl:
                    # Keep only kwargs the tool actually accepts — models sometimes invent
                    # extra args (e.g. a `limit` on a no-arg tool), which would otherwise
                    # raise a TypeError and abort the agent turn.
                    accepted = set(inspect.signature(impl).parameters)
                    safe_args = {k: v for k, v in args.items() if k in accepted}
                    try:
                        result = impl(**safe_args)
                    except Exception as e:  # a single tool failure shouldn't kill the turn
                        result = {"error": f"{name} failed: {e}"}
                else:
                    result = {"error": f"unknown tool {name}"}
                trace.append({"tool": name, "args": args, "result": result})
                messages.append({"role": "tool", "tool_call_id": tc.get("id", name),
                                 "name": name, "content": json.dumps(result, default=str)[:6000]})
        # Ran out of steps — ask for a final answer without tools.
        final = chat(messages)
        return {"reply": (final.get("content") or "").strip() or
                "I gathered data but couldn't summarise — see the trace.", "trace": trace}
    except requests.HTTPError as e:
        raise HTTPException(502, f"LLM/tool call failed: {e}")
    except Exception as e:
        raise HTTPException(502, f"agent error: {e}")


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/", StaticFiles(directory=STATIC_DIR), name="static")
