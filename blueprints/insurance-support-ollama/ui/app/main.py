"""
Insurance Support Copilot — FastAPI backend.

A support chatbot for a (synthetic) insurance company:
  * multi-turn chat with a support-agent persona, optional accident-photo upload
    (sent to a vision model over the OpenAI-compatible API),
  * open / close support tickets — the model proposes an action (function-calling)
    and the user confirms it, which does a deterministic Postgres write,
  * suggest SIMILAR past cases (semantic text search over Milvus, and image
    similarity via CLIP) — every retrieved case is REDACTED with Presidio before
    it is shown, so one customer never sees another's PII.

Config (env):
  CHAT_BASE_URL   http://localhost:8011/v1   OpenAI-compatible chat/vision endpoint
  CHAT_MODEL      ""                          default model (else first advertised)
  EMBED_BASE_URL  http://localhost:8011/v1   OpenAI-compatible /embeddings
  EMBED_MODEL     nomic-embed-text
  MILVUS_URI      http://localhost:19530
  POSTGRES_URI    postgresql://insurance:insurance@localhost:5432/insurance
  CASES_COLLECTION support_cases             (text/semantic index, built by Airflow)
  PHOTO_COLLECTION support_photos            (image similarity, populated by the UI)
  CLIP_MODEL      clip-ViT-B-32

Demo only — synthetic data; Presidio redaction is best-effort, not a guarantee.
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
import uuid
from pathlib import Path
from typing import Optional

import requests
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image

CHAT_BASE_URL = os.environ.get("CHAT_BASE_URL", "http://localhost:8011/v1").rstrip("/")
CHAT_MODEL = os.environ.get("CHAT_MODEL", "")
# Vision model for accident photos. On the Ollama variant this differs from the chat
# model (a small vision model can't do tools), so photos are captioned by the vision
# model and the caption is fed as text to the tool-capable chat model. On the vLLM
# variant it's the same VL model. Empty -> fall back to the chat model.
VISION_MODEL = os.environ.get("VISION_MODEL", "")
EMBED_BASE_URL = os.environ.get("EMBED_BASE_URL", "http://localhost:8011/v1").rstrip("/")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "nomic-embed-text")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "EMPTY")
MILVUS_URI = os.environ.get("MILVUS_URI", "http://localhost:19530").rstrip("/")
MILVUS_TOKEN = os.environ.get("MILVUS_TOKEN", "")
POSTGRES_URI = os.environ.get("POSTGRES_URI", "postgresql://insurance:insurance@localhost:5432/insurance")
CASES_COLLECTION = os.environ.get("CASES_COLLECTION", "support_cases")
PHOTO_COLLECTION = os.environ.get("PHOTO_COLLECTION", "support_photos")
CLIP_MODEL = os.environ.get("CLIP_MODEL", "clip-ViT-B-32")
CLIP_DIM = 512
HTTP_TIMEOUT = 300

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

SYSTEM_PERSONA = (
    "You are Ava, a calm, empathetic insurance customer-support agent. Help the "
    "customer understand their policy, open or close support tickets, and explain "
    "claim decisions in plain language. Be concise and factual and never invent "
    "coverage. When you reference a similar past case, only use the redacted summary "
    "provided — never reveal another customer's personal details. To open or close a "
    "ticket, call the matching tool; the user will confirm before it is applied."
)

# OpenAI-style tool schemas the model may call. The app executes them (with user
# confirmation for writes).
TOOLS = [
    {"type": "function", "function": {
        "name": "create_ticket",
        "description": "Open a new support ticket for the customer.",
        "parameters": {"type": "object", "properties": {
            "subject": {"type": "string"},
            "body": {"type": "string", "description": "Full description of the issue."},
            "priority": {"type": "string", "enum": ["low", "medium", "high", "urgent"]},
        }, "required": ["subject", "body"]}}},
    {"type": "function", "function": {
        "name": "close_ticket",
        "description": "Close an existing support ticket.",
        "parameters": {"type": "object", "properties": {
            "ticket_id": {"type": "integer"},
            "resolution_notes": {"type": "string"},
        }, "required": ["ticket_id"]}}},
    {"type": "function", "function": {
        "name": "search_similar_cases",
        "description": "Find similar past support cases to help resolve the issue.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"},
        }, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "escalate_to_human",
        "description": "Hand the conversation to a human agent.",
        "parameters": {"type": "object", "properties": {
            "reason": {"type": "string"},
        }, "required": ["reason"]}}},
]

app = FastAPI(title="Insurance Support Copilot")


# --------------------------------------------------------------------------- Postgres
def _pg():
    import psycopg2
    import psycopg2.extras
    conn = psycopg2.connect(POSTGRES_URI)
    return conn, psycopg2.extras.RealDictCursor


def pg_query(sql: str, params=None) -> list[dict]:
    conn, cur_factory = _pg()
    try:
        with conn, conn.cursor(cursor_factory=cur_factory) as cur:
            cur.execute(sql, params or ())
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def pg_exec(sql: str, params=None):
    conn, _ = _pg()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(sql, params or ())
            return cur.fetchone()
    finally:
        conn.close()


# --------------------------------------------------------------------------- Embeddings + Milvus (REST v2)
def embed(text: str) -> Optional[list[float]]:
    try:
        r = requests.post(f"{EMBED_BASE_URL}/embeddings",
                          json={"model": EMBED_MODEL, "input": text},
                          headers={"Authorization": f"Bearer {OPENAI_API_KEY}"}, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        return r.json()["data"][0]["embedding"]
    except Exception as e:  # noqa: BLE001
        print(f"[embed] failed: {e}", flush=True)
        return None


def _milvus_headers() -> dict:
    h = {"Content-Type": "application/json"}
    if MILVUS_TOKEN:
        h["Authorization"] = f"Bearer {MILVUS_TOKEN}"
    return h


def _milvus_post(path: str, body: dict) -> dict:
    r = requests.post(f"{MILVUS_URI}{path}", json=body, headers=_milvus_headers(), timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict) and data.get("code") not in (0, None):
        raise RuntimeError(f"Milvus error on {path}: {data}")
    return data


def milvus_has(name: str) -> bool:
    try:
        return name in (_milvus_post("/v2/vectordb/collections/list", {}).get("data") or [])
    except Exception:  # noqa: BLE001
        return False


def milvus_search(name: str, vector: list[float], top_k: int, output_fields: list[str]) -> list[dict]:
    data = _milvus_post("/v2/vectordb/entities/search", {
        "collectionName": name, "data": [vector], "limit": top_k,
        "outputFields": output_fields, "searchParams": {"metricType": "COSINE"},
    })
    return data.get("data") or []


# --------------------------------------------------------------------------- CLIP (photo similarity)
_clip = None


def _clip_model():
    global _clip
    if _clip is None:
        from sentence_transformers import SentenceTransformer
        _clip = SentenceTransformer(CLIP_MODEL, device="cpu")
    return _clip


def clip_image_embed(jpeg: bytes) -> Optional[list[float]]:
    try:
        img = Image.open(io.BytesIO(jpeg)).convert("RGB")
        v = _clip_model().encode(img, convert_to_numpy=True, normalize_embeddings=True)
        return v.tolist()
    except Exception as e:  # noqa: BLE001
        print(f"[clip] embed failed: {e}", flush=True)
        return None


def ensure_photo_collection():
    if not milvus_has(PHOTO_COLLECTION):
        _milvus_post("/v2/vectordb/collections/create", {
            "collectionName": PHOTO_COLLECTION,
            "schema": {"autoID": False, "enableDynamicField": True, "fields": [
                {"fieldName": "id", "dataType": "VarChar", "isPrimary": True,
                 "elementTypeParams": {"max_length": 64}},
                {"fieldName": "vector", "dataType": "FloatVector",
                 "elementTypeParams": {"dim": CLIP_DIM}},
                {"fieldName": "caption", "dataType": "VarChar",
                 "elementTypeParams": {"max_length": 4096}},
                {"fieldName": "thumb", "dataType": "VarChar",
                 "elementTypeParams": {"max_length": 65535}},
            ]},
            "indexParams": [{"fieldName": "vector", "metricType": "COSINE",
                             "indexName": "vi", "indexType": "AUTOINDEX"}],
        })


# --------------------------------------------------------------------------- Presidio redaction
_analyzer = None
_anonymizer = None
_ENTITIES = ["PERSON", "EMAIL_ADDRESS", "PHONE_NUMBER", "CREDIT_CARD", "IBAN_CODE",
             "US_SSN", "LOCATION", "DATE_TIME", "POLICY_NUMBER", "CLAIM_ID"]


def _presidio():
    """Lazily build Presidio analyzer + anonymizer with custom insurance recognizers.
    Falls back to (None, None) if unavailable — the caller then uses regex redaction."""
    global _analyzer, _anonymizer
    if _analyzer is None:
        try:
            from presidio_analyzer import AnalyzerEngine, PatternRecognizer, Pattern
            from presidio_analyzer.nlp_engine import NlpEngineProvider
            from presidio_anonymizer import AnonymizerEngine
            provider = NlpEngineProvider(nlp_configuration={
                "nlp_engine_name": "spacy",
                "models": [{"lang_code": "en", "model_name": "en_core_web_sm"}],
            })
            analyzer = AnalyzerEngine(nlp_engine=provider.create_engine(), supported_languages=["en"])
            analyzer.registry.add_recognizer(PatternRecognizer(
                supported_entity="POLICY_NUMBER",
                patterns=[Pattern("policy", r"\bPOL-\d{4,8}\b", 0.9)]))
            analyzer.registry.add_recognizer(PatternRecognizer(
                supported_entity="CLAIM_ID",
                patterns=[Pattern("claim", r"\bCLM-\d{4,8}\b", 0.9)]))
            _analyzer, _anonymizer = analyzer, AnonymizerEngine()
        except Exception as e:  # noqa: BLE001
            print(f"[presidio] unavailable, using regex fallback: {e}", flush=True)
            _analyzer, _anonymizer = False, False
    return _analyzer, _anonymizer


_REGEX_FALLBACK = [
    (re.compile(r"\bPOL-\d{4,8}\b"), "<POLICY_NUMBER>"),
    (re.compile(r"\bCLM-\d{4,8}\b"), "<CLAIM_ID>"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "<EMAIL_ADDRESS>"),
    (re.compile(r"\b(?:\+?\d[\d\s().-]{7,}\d)\b"), "<PHONE_NUMBER>"),
    (re.compile(r"\b\d{4}[ -]?\d{4}[ -]?\d{4}[ -]?\d{4}\b"), "<CREDIT_CARD>"),
]


def redact(text: str) -> str:
    if not text:
        return text
    analyzer, anonymizer = _presidio()
    if analyzer and anonymizer:
        try:
            results = analyzer.analyze(text=text, entities=_ENTITIES, language="en")
            return anonymizer.anonymize(text=text, analyzer_results=results).text
        except Exception as e:  # noqa: BLE001
            print(f"[presidio] analyze failed, regex fallback: {e}", flush=True)
    out = text
    for rx, repl in _REGEX_FALLBACK:
        out = rx.sub(repl, out)
    return out


# --------------------------------------------------------------------------- images
def _as_jpeg(raw: bytes, max_side: int = 1024) -> bytes:
    img = Image.open(io.BytesIO(raw)).convert("RGB")
    w, h = img.size
    s = max_side / float(max(w, h))
    if s < 1.0:
        img = img.resize((int(w * s), int(h * s)))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88)
    return buf.getvalue()


def _thumb_b64(jpeg: bytes, max_side: int = 320) -> str:
    img = Image.open(io.BytesIO(jpeg)).convert("RGB")
    w, h = img.size
    s = max_side / float(max(w, h))
    if s < 1.0:
        img = img.resize((int(w * s), int(h * s)))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=70)
    return base64.b64encode(buf.getvalue()).decode()


def _b64(jpeg: bytes) -> str:
    return base64.b64encode(jpeg).decode()


# --------------------------------------------------------------------------- chat / models
def list_models() -> list[str]:
    try:
        r = requests.get(f"{CHAT_BASE_URL}/models",
                         headers={"Authorization": f"Bearer {OPENAI_API_KEY}"}, timeout=15)
        r.raise_for_status()
        return [m["id"] for m in r.json().get("data", []) if m.get("id")]
    except Exception as e:  # noqa: BLE001
        print(f"[models] list failed: {e}", flush=True)
        return []


def _default_model() -> str:
    served = list_models()
    if CHAT_MODEL:
        return CHAT_MODEL
    # prefer the customized persona model if present
    for m in served:
        if "support-agent" in m:
            return m
    return served[0] if served else ""


# Models the endpoint has told us don't support tools (e.g. many vision models on
# Ollama) — we stop sending tools to them and fall back to the guided UI ticket forms.
_no_tools: set[str] = set()


def chat_completion(messages: list[dict], model: str, use_tools: bool) -> dict:
    def _post(with_tools: bool):
        payload = {"model": model, "messages": messages, "temperature": 0.2, "max_tokens": 700}
        if with_tools:
            payload["tools"] = TOOLS
        return requests.post(f"{CHAT_BASE_URL}/chat/completions", json=payload,
                             headers={"Authorization": f"Bearer {OPENAI_API_KEY}"}, timeout=HTTP_TIMEOUT)

    want_tools = use_tools and model not in _no_tools
    r = _post(want_tools)
    # Some models don't support tools ("... does not support tools") — retry once
    # without them, remember the model, and let ticket actions use the sidebar forms.
    if r.status_code >= 400 and want_tools and "tool" in r.text.lower():
        _no_tools.add(model)
        r = _post(False)
    if r.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"chat call failed: {r.text[:300]}")
    return r.json()["choices"][0]["message"]


def describe_photo(jpeg: bytes, model: str) -> str:
    """Caption an accident photo with the vision model (no tools)."""
    msg = chat_completion([
        {"role": "system", "content": "You are an insurance claims assistant. Describe the "
         "damage visible in this photo factually in 2-3 sentences for a claim note."},
        {"role": "user", "content": [
            {"type": "text", "text": "Describe the damage in this photo."},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{_b64(jpeg)}"}},
        ]},
    ], model, use_tools=False)
    return (msg.get("content") or "").strip()


# --------------------------------------------------------------------------- routes
@app.get("/api/health")
def health():
    ok_pg = True
    try:
        pg_query("SELECT 1 AS ok")
    except Exception:  # noqa: BLE001
        ok_pg = False
    return {
        "ok": True, "chat_endpoint": CHAT_BASE_URL, "models": list_models(),
        "embed_model": EMBED_MODEL, "milvus": milvus_has(CASES_COLLECTION),
        "cases_collection": CASES_COLLECTION, "postgres": ok_pg,
    }


@app.get("/api/models")
def models():
    return {"default": _default_model(), "models": list_models()}


@app.post("/api/chat")
async def chat(
    message: str = Form(...),
    history: str = Form("[]"),
    model: str = Form(""),
    file: Optional[UploadFile] = File(None),
):
    """One assistant turn. `history` is a JSON list of prior {role,content} turns.
    If the model requests a write tool (create/close ticket), we return it as a
    `proposed_action` for the UI to confirm rather than executing it here."""
    mdl = model.strip() or _default_model()
    if not mdl:
        raise HTTPException(status_code=503, detail="no model available at the chat endpoint")
    try:
        prior = json.loads(history or "[]")
    except Exception:  # noqa: BLE001
        prior = []

    # If a photo is attached, caption it with the vision model, then feed the caption
    # as TEXT to the (tool-capable) chat model — so the chat model never needs to be
    # multimodal AND tool-capable at once.
    text = message
    if file is not None:
        jpeg = _as_jpeg(await file.read())
        try:
            caption = describe_photo(jpeg, VISION_MODEL or mdl)
            text = (message + f"\n\n[Attached accident photo — visual description: {caption}]").strip()
        except HTTPException:
            text = (message + "\n\n[An accident photo was attached but could not be analysed.]").strip()

    messages = [{"role": "system", "content": SYSTEM_PERSONA}, *prior,
                {"role": "user", "content": text}]
    msg = chat_completion(messages, mdl, use_tools=True)

    proposed = None
    cases = None
    for tc in (msg.get("tool_calls") or []):
        fn = tc.get("function", {})
        name = fn.get("name")
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except Exception:  # noqa: BLE001
            args = {}
        if name in ("create_ticket", "close_ticket"):
            proposed = {"name": name, "arguments": args}  # UI confirms → write
        elif name == "search_similar_cases":
            cases = semantic_cases(args.get("query", message), 5)
        elif name == "escalate_to_human":
            proposed = {"name": "escalate_to_human", "arguments": args}

    return {"reply": msg.get("content") or "", "model": mdl,
            "proposed_action": proposed, "cases": cases}


# ---- tickets (deterministic writes; called on user confirm or from the UI forms) ----
@app.post("/api/tickets/create")
def create_ticket(subject: str = Form(...), body: str = Form(...),
                  priority: str = Form("medium"), customer_id: int = Form(0),
                  policy_id: int = Form(0)):
    row = pg_exec("""
        INSERT INTO support_tickets
            (ticket_id, customer_id, policy_id, subject, body, channel, priority, status,
             created_at, updated_at)
        VALUES ((SELECT COALESCE(MAX(ticket_id),0)+1 FROM support_tickets),
                NULLIF(%s,0), NULLIF(%s,0), %s, %s, 'chat', %s, 'open', NOW(), NOW())
        RETURNING ticket_id
    """, (customer_id, policy_id, subject, body, priority))
    return {"ticket_id": row[0] if row else None, "status": "open"}


@app.post("/api/tickets/close")
def close_ticket(ticket_id: int = Form(...), resolution_notes: str = Form("")):
    row = pg_exec("""
        UPDATE support_tickets
        SET status='closed', resolved_at=COALESCE(resolved_at, NOW()), closed_at=NOW(),
            updated_at=NOW(), resolution_notes=%s
        WHERE ticket_id=%s
        RETURNING ticket_id
    """, (resolution_notes, ticket_id))
    if not row:
        raise HTTPException(status_code=404, detail=f"ticket {ticket_id} not found")
    return {"ticket_id": ticket_id, "status": "closed"}


@app.get("/api/tickets")
def list_tickets(status: str = "", limit: int = 20):
    if status:
        return {"tickets": pg_query(
            "SELECT ticket_id, subject, status, priority, created_at FROM support_tickets "
            "WHERE status=%s ORDER BY ticket_id DESC LIMIT %s", (status, limit))}
    return {"tickets": pg_query(
        "SELECT ticket_id, subject, status, priority, created_at FROM support_tickets "
        "ORDER BY ticket_id DESC LIMIT %s", (limit,))}


@app.get("/api/tickets/{ticket_id}")
def get_ticket(ticket_id: int):
    rows = pg_query("SELECT * FROM support_tickets WHERE ticket_id=%s", (ticket_id,))
    if not rows:
        raise HTTPException(status_code=404, detail=f"ticket {ticket_id} not found")
    return rows[0]


# ---- similar-case search (redacted) ----
def semantic_cases(query: str, top_k: int) -> list[dict]:
    if not milvus_has(CASES_COLLECTION):
        return []
    vec = embed(query)
    if vec is None:
        return []
    hits = milvus_search(CASES_COLLECTION, vec, top_k,
                         ["subject", "body", "accident_type", "product_type",
                          "status", "was_paid", "within_policy", "resolution"])
    out = []
    for h in hits:
        out.append({
            "score": round(float(h.get("distance", 0)), 4),
            "subject": redact(h.get("subject", "")),
            "body": redact(h.get("body", "")),
            "resolution": redact(h.get("resolution", "")),
            "accident_type": h.get("accident_type", ""),
            "product_type": h.get("product_type", ""),
            "was_paid": bool(h.get("was_paid")),
            "within_policy": bool(h.get("within_policy")),
        })
    return out


@app.post("/api/search/semantic")
def search_semantic(query: str = Form(...), top_k: int = Form(5)):
    return {"query": query, "cases": semantic_cases(query, top_k)}


@app.post("/api/search/similar")
async def search_similar(top_k: int = Form(5), file: UploadFile = File(...)):
    jpeg = _as_jpeg(await file.read())
    vec = clip_image_embed(jpeg)
    if vec is None:
        raise HTTPException(status_code=503, detail="CLIP unavailable")
    if not milvus_has(PHOTO_COLLECTION):
        return {"cases": [], "note": "no indexed photos yet — analyse a few first"}
    hits = milvus_search(PHOTO_COLLECTION, vec, top_k, ["caption", "thumb"])
    return {"cases": [{"score": round(float(h.get("distance", 0)), 4),
                       "caption": redact(h.get("caption", "")),
                       "thumb": h.get("thumb", "")} for h in hits]}


@app.post("/api/analyze-photo")
async def analyze_photo(file: UploadFile = File(...), model: str = Form("")):
    """Describe an accident photo with the vision model and index it (CLIP) so it
    becomes searchable by image similarity."""
    jpeg = _as_jpeg(await file.read())
    mdl = model.strip() or VISION_MODEL or _default_model()
    caption = describe_photo(jpeg, mdl)
    vec = clip_image_embed(jpeg)
    indexed = False
    if vec is not None:
        try:
            ensure_photo_collection()
            _milvus_post("/v2/vectordb/entities/insert", {
                "collectionName": PHOTO_COLLECTION,
                "data": [{"id": uuid.uuid4().hex[:16], "vector": vec,
                          "caption": caption[:4096], "thumb": _thumb_b64(jpeg)}],
            })
            indexed = True
        except Exception as e:  # noqa: BLE001
            print(f"[photo index] failed: {e}", flush=True)
    return {"caption": caption, "model": mdl, "indexed": indexed, "thumb": _b64(jpeg)}


@app.exception_handler(HTTPException)
async def _http_exc(_, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})


# Static SUSE-styled frontend (mounted last so /api/* wins).
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
