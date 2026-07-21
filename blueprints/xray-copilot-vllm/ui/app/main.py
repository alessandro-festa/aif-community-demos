"""
Chest X-ray Copilot — minimal FastAPI UI.

Upload (or pick a sample) chest X-ray, get an analysis from a medical
vision-language model over the OpenAI-compatible API (MedGemma / LLaVA-Med served
by vLLM or Ollama), then embed the image with BiomedCLIP (CPU, in-process) and
store it in Milvus so you can do:
  * similarity search  — image -> nearest stored X-rays,
  * semantic search    — text query -> nearest stored X-rays (shared CLIP space).

Everything except the LLM + Milvus runs locally in this process on CPU.

Configuration (env):
  OPENAI_BASE_URL  default http://localhost:8000/v1   (vLLM router / Ollama)
  OPENAI_API_KEY   default EMPTY
  DEFAULT_MODEL    default ""  (else first model the endpoint advertises)
  MILVUS_URI       default http://localhost:19530
  CLIP_MODEL       default hf-hub:microsoft/BiomedCLIP-PubMedBERT_256-vit_base_patch16_224
  IMG_COLLECTION   default xray_embeddings

⚠️ Research/demo only — NOT a medical device and NOT for clinical use.
"""
from __future__ import annotations

import base64
import io
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

OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "http://localhost:8000/v1").rstrip("/")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "EMPTY")
DEFAULT_MODEL = os.environ.get("DEFAULT_MODEL", "")
MILVUS_URI = os.environ.get("MILVUS_URI", "http://localhost:19530")
CLIP_MODEL = os.environ.get(
    "CLIP_MODEL", "hf-hub:microsoft/BiomedCLIP-PubMedBERT_256-vit_base_patch16_224")
COLLECTION = os.environ.get("IMG_COLLECTION", "xray_embeddings")
CLIP_DIM = 512  # BiomedCLIP projected embedding
HTTP_TIMEOUT = 600

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
SAMPLES_DIR = STATIC_DIR / "samples"

SYSTEM_PROMPT = (
    "You are a radiology assistant reviewing a single chest X-ray for a teaching "
    "demo. Report systematically and concisely:\n"
    "1. Image: view/projection and technical quality.\n"
    "2. Findings by region: lungs, heart & mediastinum, pleura, bones/soft tissue, "
    "and any lines/tubes/devices.\n"
    "3. Impression: the most likely finding(s) in one or two lines.\n"
    "Be specific and factual; describe only what is visible. End with: "
    "'Demo only — not a diagnosis.'"
)

app = FastAPI(title="Chest X-ray Copilot")


# --------------------------------------------------------------------------- CLIP
_clip = None  # (model, preprocess, tokenizer)


def _clip_load():
    global _clip
    if _clip is None:
        import torch  # noqa: F401  (ensures a clear error if torch is missing)
        from open_clip import create_model_from_pretrained, get_tokenizer
        model, preprocess = create_model_from_pretrained(CLIP_MODEL)
        tokenizer = get_tokenizer(CLIP_MODEL)
        model.eval()
        _clip = (model, preprocess, tokenizer)
    return _clip


def clip_image_embed(img_bytes: bytes) -> Optional[list[float]]:
    try:
        import torch
        model, preprocess, _ = _clip_load()
        img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
        with torch.no_grad():
            t = preprocess(img).unsqueeze(0)
            f = model.encode_image(t)
            f = f / f.norm(dim=-1, keepdim=True)
        return f[0].cpu().tolist()
    except Exception as e:  # noqa: BLE001
        print(f"[clip] image embed failed: {e}", flush=True)
        return None


def clip_text_embed(text: str) -> Optional[list[float]]:
    try:
        import torch
        model, _, tokenizer = _clip_load()
        with torch.no_grad():
            toks = tokenizer([text], context_length=256)
            f = model.encode_text(toks)
            f = f / f.norm(dim=-1, keepdim=True)
        return f[0].cpu().tolist()
    except Exception as e:  # noqa: BLE001
        print(f"[clip] text embed failed: {e}", flush=True)
        return None


# ------------------------------------------------------------------------ Milvus
_milvus_ready = False


def _milvus():
    """Connect to Milvus and ensure the X-ray collection exists. Returns the
    collection, or None if Milvus is unreachable (indexing is best-effort)."""
    global _milvus_ready
    try:
        from pymilvus import (Collection, CollectionSchema, DataType, FieldSchema,
                              connections, utility)
        if not _milvus_ready:
            host = MILVUS_URI.replace("http://", "").replace("https://", "")
            h, _, p = host.partition(":")
            connections.connect(alias="default", host=h, port=p or "19530")
            if not utility.has_collection(COLLECTION):
                fields = [
                    FieldSchema(name="id", dtype=DataType.VARCHAR, is_primary=True, max_length=64),
                    FieldSchema(name="filename", dtype=DataType.VARCHAR, max_length=512),
                    FieldSchema(name="model", dtype=DataType.VARCHAR, max_length=128),
                    FieldSchema(name="analysis", dtype=DataType.VARCHAR, max_length=8192),
                    FieldSchema(name="thumb", dtype=DataType.VARCHAR, max_length=65535),
                    FieldSchema(name="vector", dtype=DataType.FLOAT_VECTOR, dim=CLIP_DIM),
                ]
                col = Collection(COLLECTION, CollectionSchema(fields, description="Chest X-ray BiomedCLIP index"))
                col.create_index("vector", {"index_type": "AUTOINDEX", "metric_type": "COSINE"})
            _milvus_ready = True
        col = Collection(COLLECTION)
        col.load()
        return col
    except Exception as e:  # noqa: BLE001
        print(f"[milvus] unavailable: {e}", flush=True)
        return None


def _store(filename: str, model: str, analysis: str, jpeg: bytes, vec: list[float]) -> bool:
    col = _milvus()
    if col is None or vec is None:
        return False
    try:
        col.insert([{
            "id": uuid.uuid4().hex[:16],
            "filename": filename[:512],
            "model": model[:128],
            "analysis": (analysis or "")[:8192],
            "thumb": _thumb_b64(jpeg),
            "vector": vec,
        }])
        col.flush()
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[milvus] insert failed: {e}", flush=True)
        return False


def _hits(col, vec: list[float], top_k: int) -> list[dict]:
    res = col.search(
        data=[vec], anns_field="vector", param={"metric_type": "COSINE"},
        limit=max(1, min(int(top_k), 24)),
        output_fields=["filename", "model", "analysis", "thumb"],
    )
    out = []
    for hit in res[0]:
        out.append({
            "score": round(float(hit.distance), 4),
            "filename": hit.entity.get("filename"),
            "model": hit.entity.get("model"),
            "analysis": hit.entity.get("analysis"),
            "thumb": hit.entity.get("thumb"),
        })
    return out


# -------------------------------------------------------------------------- image
def _as_jpeg(raw: bytes, max_side: int = 1024) -> bytes:
    """Decode any supported image and re-encode as a bounded RGB JPEG."""
    img = Image.open(io.BytesIO(raw)).convert("RGB")
    w, h = img.size
    s = max_side / float(max(w, h))
    if s < 1.0:
        img = img.resize((int(w * s), int(h * s)))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88)
    return buf.getvalue()


def _thumb_b64(jpeg: bytes, max_side: int = 320, quality: int = 70) -> str:
    img = Image.open(io.BytesIO(jpeg)).convert("RGB")
    w, h = img.size
    s = max_side / float(max(w, h))
    if s < 1.0:
        img = img.resize((int(w * s), int(h * s)))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return base64.b64encode(buf.getvalue()).decode()


def _b64(jpeg: bytes) -> str:
    return base64.b64encode(jpeg).decode()


async def _read_image(file: Optional[UploadFile], sample: str) -> tuple[bytes, str]:
    """Return (jpeg_bytes, display_name) from either an upload or a bundled sample."""
    if file is not None:
        raw = await file.read()
        return _as_jpeg(raw), (file.filename or "upload")
    if sample:
        p = (SAMPLES_DIR / sample).resolve()
        if SAMPLES_DIR not in p.parents or not p.is_file():
            raise HTTPException(status_code=400, detail=f"unknown sample: {sample}")
        return _as_jpeg(p.read_bytes()), sample
    raise HTTPException(status_code=400, detail="no image provided (upload a file or choose a sample)")


# -------------------------------------------------------------------------- models
def list_models() -> list[str]:
    try:
        r = requests.get(f"{OPENAI_BASE_URL}/models",
                         headers={"Authorization": f"Bearer {OPENAI_API_KEY}"}, timeout=15)
        r.raise_for_status()
        return [m["id"] for m in r.json().get("data", []) if m.get("id")]
    except Exception as e:  # noqa: BLE001
        print(f"[models] list failed: {e}", flush=True)
        return []


_FENCE_RE = re.compile(r"^```[a-zA-Z]*\n|\n```$")


def analyse(jpeg: bytes, model: str, question: str) -> str:
    user_text = question.strip() or "Analyse this chest X-ray."
    payload = {
        "model": model,
        "temperature": 0,
        "max_tokens": 700,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": [
                {"type": "text", "text": user_text},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{_b64(jpeg)}"}},
            ]},
        ],
    }
    r = requests.post(f"{OPENAI_BASE_URL}/chat/completions", json=payload,
                      headers={"Authorization": f"Bearer {OPENAI_API_KEY}"}, timeout=HTTP_TIMEOUT)
    if r.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"model call failed: {r.text[:300]}")
    content = r.json()["choices"][0]["message"]["content"]
    return _FENCE_RE.sub("", (content or "").strip())


# -------------------------------------------------------------------------- routes
@app.get("/api/health")
def health():
    col = _milvus()
    count = None
    if col is not None:
        try:
            count = col.num_entities
        except Exception:  # noqa: BLE001
            count = None
    return {
        "ok": True,
        "endpoint": OPENAI_BASE_URL,
        "models": list_models(),
        "clip_model": CLIP_MODEL,
        "milvus": col is not None,
        "collection": COLLECTION,
        "indexed": count,
    }


@app.get("/api/models")
def models():
    served = list_models()
    default = DEFAULT_MODEL or (served[0] if served else "")
    return {"default": default, "models": served}


@app.get("/api/samples")
def samples():
    items = []
    if SAMPLES_DIR.is_dir():
        for p in sorted(SAMPLES_DIR.iterdir()):
            if p.suffix.lower() in (".png", ".jpg", ".jpeg"):
                items.append({"name": p.name, "url": f"/samples/{p.name}",
                              "label": p.stem.replace("-", " ").replace("_", " ")})
    return {"samples": items}


@app.post("/api/analyze")
async def analyze(
    model: str = Form(""),
    question: str = Form(""),
    sample: str = Form(""),
    file: Optional[UploadFile] = File(None),
):
    jpeg, name = await _read_image(file, sample)
    mdl = model.strip() or DEFAULT_MODEL or (list_models() or [""])[0]
    if not mdl:
        raise HTTPException(status_code=503, detail="no model available at the endpoint")
    analysis = analyse(jpeg, mdl, question)
    vec = clip_image_embed(jpeg)
    stored = _store(name, mdl, analysis, jpeg, vec) if vec is not None else False
    return {"analysis": analysis, "model": mdl, "filename": name,
            "stored": stored, "thumb": _b64(jpeg)}


@app.post("/api/search/similar")
async def search_similar(
    top_k: int = Form(8),
    sample: str = Form(""),
    file: Optional[UploadFile] = File(None),
):
    jpeg, name = await _read_image(file, sample)
    col = _milvus()
    if col is None:
        raise HTTPException(status_code=503, detail="Milvus is not available")
    vec = clip_image_embed(jpeg)
    if vec is None:
        raise HTTPException(status_code=503, detail="BiomedCLIP unavailable")
    return {"query": name, "hits": _hits(col, vec, top_k)}


@app.post("/api/search/semantic")
def search_semantic(query: str = Form(...), top_k: int = Form(8)):
    col = _milvus()
    if col is None:
        raise HTTPException(status_code=503, detail="Milvus is not available")
    vec = clip_text_embed(query)
    if vec is None:
        raise HTTPException(status_code=503, detail="BiomedCLIP unavailable")
    return {"query": query, "hits": _hits(col, vec, top_k)}


@app.exception_handler(HTTPException)
async def _http_exc(_, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})


# Static SUSE-styled frontend (mounted last so /api/* and /samples/* win first).
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
