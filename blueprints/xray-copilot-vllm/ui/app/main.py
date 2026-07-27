"""
Chest X-ray Copilot — minimal FastAPI UI.

Upload (or pick a sample) chest X-ray, get an analysis from a medical
vision-language model over the OpenAI-compatible API (MedGemma / LLaVA-Med served
by vLLM or Ollama), then embed the image with BiomedCLIP (CPU, in-process) and
store it in Qdrant so you can do:
  * similarity search  — image -> nearest stored X-rays,
  * semantic search    — text query -> nearest stored X-rays (shared CLIP space).

Everything except the LLM + Qdrant runs locally in this process on CPU.

Configuration (env):
  OPENAI_BASE_URL  default http://localhost:8000/v1   (vLLM router / Ollama)
  OPENAI_API_KEY   default EMPTY
  DEFAULT_MODEL    default ""  (else first model the endpoint advertises)
  QDRANT_URL       default http://localhost:6333
  QDRANT_API_KEY   default ""  (only sent as the `api-key` header if non-empty)
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
QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333").rstrip("/")
QDRANT_API_KEY = os.environ.get("QDRANT_API_KEY", "")
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


# ------------------------------------------------------------------------ Qdrant
_qdrant_ready = False


def _qdrant_headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if QDRANT_API_KEY:
        headers["api-key"] = QDRANT_API_KEY
    return headers


def _qdrant_ensure_collection() -> bool:
    """Make sure the X-ray collection exists in Qdrant. Returns True if Qdrant is
    reachable and the collection is ready, False otherwise (indexing/search is
    best-effort)."""
    global _qdrant_ready
    if _qdrant_ready:
        return True
    try:
        r = requests.get(f"{QDRANT_URL}/collections/{COLLECTION}",
                         headers=_qdrant_headers(), timeout=10)
        if r.status_code == 404:
            r = requests.put(
                f"{QDRANT_URL}/collections/{COLLECTION}",
                headers=_qdrant_headers(),
                json={"vectors": {"size": CLIP_DIM, "distance": "Cosine"}},
                timeout=30,
            )
            r.raise_for_status()
        else:
            r.raise_for_status()
        _qdrant_ready = True
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[qdrant] unavailable: {e}", flush=True)
        return False


def _qdrant_status() -> tuple[bool, Optional[int]]:
    """Return (available, indexed_point_count)."""
    if not _qdrant_ensure_collection():
        return False, None
    try:
        r = requests.get(f"{QDRANT_URL}/collections/{COLLECTION}",
                         headers=_qdrant_headers(), timeout=10)
        r.raise_for_status()
        return True, r.json().get("result", {}).get("points_count")
    except Exception as e:  # noqa: BLE001
        print(f"[qdrant] status failed: {e}", flush=True)
        return False, None


def _store(filename: str, model: str, analysis: str, jpeg: bytes, vec: list[float]) -> bool:
    if vec is None or not _qdrant_ensure_collection():
        return False
    try:
        point = {
            "id": str(uuid.uuid4()),
            "vector": vec,
            "payload": {
                "filename": filename[:512],
                "model": model[:128],
                "analysis": (analysis or "")[:8192],
                "thumb": _thumb_b64(jpeg),
            },
        }
        r = requests.put(
            f"{QDRANT_URL}/collections/{COLLECTION}/points",
            headers=_qdrant_headers(), json={"points": [point]}, timeout=30,
        )
        r.raise_for_status()
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[qdrant] insert failed: {e}", flush=True)
        return False


def _hits(vec: list[float], top_k: int) -> list[dict]:
    r = requests.post(
        f"{QDRANT_URL}/collections/{COLLECTION}/points/search",
        headers=_qdrant_headers(),
        json={"vector": vec, "limit": max(1, min(int(top_k), 24)), "with_payload": True},
        timeout=30,
    )
    r.raise_for_status()
    out = []
    for hit in r.json().get("result", []):
        payload = hit.get("payload") or {}
        out.append({
            "score": round(float(hit.get("score", 0.0)), 4),
            "filename": payload.get("filename"),
            "model": payload.get("model"),
            "analysis": payload.get("analysis"),
            "thumb": payload.get("thumb"),
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
    available, count = _qdrant_status()
    return {
        "ok": True,
        "endpoint": OPENAI_BASE_URL,
        "models": list_models(),
        "clip_model": CLIP_MODEL,
        "qdrant": available,
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
    if not _qdrant_ensure_collection():
        raise HTTPException(status_code=503, detail="Qdrant is not available")
    vec = clip_image_embed(jpeg)
    if vec is None:
        raise HTTPException(status_code=503, detail="BiomedCLIP unavailable")
    return {"query": name, "hits": _hits(vec, top_k)}


@app.post("/api/search/semantic")
def search_semantic(query: str = Form(...), top_k: int = Form(8)):
    if not _qdrant_ensure_collection():
        raise HTTPException(status_code=503, detail="Qdrant is not available")
    vec = clip_text_embed(query)
    if vec is None:
        raise HTTPException(status_code=503, detail="BiomedCLIP unavailable")
    return {"query": query, "hits": _hits(vec, top_k)}


@app.exception_handler(HTTPException)
async def _http_exc(_, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})


# Static SUSE-styled frontend (mounted last so /api/* and /samples/* win first).
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
