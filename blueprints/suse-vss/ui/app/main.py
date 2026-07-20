"""
SUSE VSC — minimal Video Search & Summarization UI.

A small FastAPI backend that, all on CPU and with non-NVIDIA services:
  * ingests a video from URL / upload / webcam / YouTube / RTSP,
  * samples N frames with OpenCV — N and the frame resolution adapt to the
    machine's CPU so it stays fast on small nodes,
  * captions/answers a chosen prompt against each frame using an OpenAI-compatible
    multimodal model (SUSE Ollama, default moondream:1.8b), streaming results live,
  * stores a CLIP image embedding + thumbnail + metadata for each frame in Milvus,
  * lets you search videos by text — CLIP matches the *image* to your query
    (text→image semantic search), returning the frame and its metadata.

Configuration (env):
  OLLAMA_BASE_URL  default http://ollama:11434/v1
  VLM_MODEL        default moondream:1.8b
  CLIP_MODEL       default clip-ViT-B-32   (sentence-transformers; CPU)
  MILVUS_URI       default http://milvus:19530
  FRAME_COUNT      default 0 = auto (adapt to CPU); >0 forces a fixed count
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
import requests
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image

OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://ollama:11434/v1").rstrip("/")
VLM_MODEL = os.environ.get("VLM_MODEL", "moondream:1.8b")
CLIP_MODEL = os.environ.get("CLIP_MODEL", "clip-ViT-B-32")
MILVUS_URI = os.environ.get("MILVUS_URI", "http://milvus:19530")
FRAME_COUNT = int(os.environ.get("FRAME_COUNT", "0"))  # 0 = auto (adapt to CPU)
COLLECTION = "vss_clip"
CLIP_DIM = 512  # clip-ViT-B-32

# Ollama root (for /api/tags, /api/show, /api/pull — not under the OpenAI /v1 path).
OLLAMA_ROOT = OLLAMA_BASE_URL[:-3] if OLLAMA_BASE_URL.endswith("/v1") else OLLAMA_BASE_URL
# CPU-friendly multimodal models suggested in the UI.
SUGGESTED_VLMS = ["moondream:1.8b", "llava-phi3", "llava:7b", "gemma3:4b", "minicpm-v"]

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# Pre-baked prompts — tight, single-task and grounded so small VLMs follow them.
PROMPTS = [
    {"id": "describe", "label": "Scene description",
     "text": "Describe only what is clearly visible in this image in one short, factual "
             "sentence — the main subject(s), the setting, and any action. Do not guess or "
             "mention anything you cannot see."},
    {"id": "objects", "label": "Object detection",
     "text": "List the distinct physical objects that are clearly visible in this image as a "
             "comma-separated list of plain nouns (e.g. 'car, traffic light, person'). No "
             "sentences, no adjectives, no explanations. If nothing is clearly identifiable, "
             "reply exactly: none."},
    {"id": "safety", "label": "Safety / PPE",
     "text": "Act as a workplace-safety inspector and look only at this image. For each person "
             "visible, state whether they are wearing a hard hat, hi-vis vest and gloves, and "
             "name any visible hazard (spill, fire, fall risk, blocked exit). Be specific and "
             "under 25 words. If no person is visible, reply exactly: no people visible."},
    {"id": "ocr", "label": "Text / OCR",
     "text": "Transcribe verbatim only the text that is clearly legible in this image, keeping "
             "line breaks. Output the text and nothing else — no quotes, no commentary. If "
             "there is no legible text, reply exactly: no text."},
    {"id": "activity", "label": "Activity summary",
     "text": "Name the single main action or activity happening in this image in a short noun "
             "phrase (e.g. 'a person riding a bicycle'). Base it only on what is visible. If no "
             "clear activity, reply exactly: no activity."},
    {"id": "anomaly", "label": "Anomaly detection",
     "text": "Look only at this image and report anything unusual, unsafe or out of place "
             "(e.g. smoke, fire, a spill, a fallen person, an intruder, damage). Answer in one "
             "specific sentence. If nothing is unusual, reply exactly: nothing unusual."},
    {"id": "count", "label": "Count people",
     "text": "Count the people clearly visible in this image. Reply with just the number "
             "(digits). If you cannot tell, reply exactly: unclear."},
]
PROMPT_BY_ID = {p["id"]: p["text"] for p in PROMPTS}

app = FastAPI(title="SUSE VSC")


# --------------------------------------------------------------------------- CPU
def cpu_count_effective() -> float:
    """Usable CPU count, honouring the container's cgroup CPU quota when set."""
    try:  # cgroup v2
        with open("/sys/fs/cgroup/cpu.max") as f:
            quota, period = f.read().split()
            if quota != "max":
                return max(1.0, int(quota) / int(period))
    except Exception:  # noqa: BLE001
        pass
    try:
        return float(len(os.sched_getaffinity(0)))  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return float(os.cpu_count() or 1)


def auto_frame_count() -> int:
    c = cpu_count_effective()
    if c <= 2:
        return 4
    if c <= 4:
        return 6
    if c <= 8:
        return 8
    return 12


def auto_max_side() -> int:
    """Frame resolution (longest side) scaled to CPU — smaller is faster."""
    c = cpu_count_effective()
    if c <= 2:
        return 448
    if c <= 4:
        return 512
    return 640


def auto_interval() -> float:
    """Suggested seconds between sampled frames, scaled to CPU. Each frame is one
    VLM call, so weaker CPUs sample more sparsely to keep the run fast enough."""
    c = cpu_count_effective()
    if c <= 2:
        return 5.0
    if c <= 4:
        return 3.0
    if c <= 8:
        return 2.0
    return 1.0


# ------------------------------------------------------------------------- CLIP
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
        print(f"[clip] image embed failed: {e}", flush=True)
        return None


def clip_text_embed(text: str) -> Optional[list[float]]:
    try:
        v = _clip_model().encode(text, convert_to_numpy=True, normalize_embeddings=True)
        return v.tolist()
    except Exception as e:  # noqa: BLE001
        print(f"[clip] text embed failed: {e}", flush=True)
        return None


# ------------------------------------------------------------------------ Milvus
_milvus_ready = False


def _milvus():
    """Connect to Milvus and ensure the CLIP collection exists. Returns the
    collection, or None if Milvus is unreachable."""
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
                    FieldSchema(name="video_id", dtype=DataType.VARCHAR, max_length=64),
                    FieldSchema(name="ts", dtype=DataType.FLOAT),
                    FieldSchema(name="source", dtype=DataType.VARCHAR, max_length=1024),
                    FieldSchema(name="caption", dtype=DataType.VARCHAR, max_length=4096),
                    FieldSchema(name="thumb", dtype=DataType.VARCHAR, max_length=65535),
                    FieldSchema(name="vector", dtype=DataType.FLOAT_VECTOR, dim=CLIP_DIM),
                ]
                col = Collection(COLLECTION, CollectionSchema(fields, description="VSS CLIP frame index"))
                col.create_index("vector", {"index_type": "AUTOINDEX", "metric_type": "COSINE"})
            _milvus_ready = True
        col = Collection(COLLECTION)
        col.load()
        return col
    except Exception as e:  # noqa: BLE001 — best-effort indexing
        print(f"[milvus] unavailable: {e}", flush=True)
        return None


# ------------------------------------------------------------------------ frames
def _resize_frame(frame, max_side: int):
    """Downscale a BGR frame so its longest side <= max_side (no upscaling)."""
    h, w = frame.shape[:2]
    s = max_side / float(max(h, w))
    if s < 1.0:
        frame = cv2.resize(frame, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA)
    return frame


def _encode(frame, quality: int = 82) -> bytes:
    ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    return buf.tobytes() if ok else b""


def _thumb_b64(jpeg: bytes, max_side: int = 384, quality: int = 70) -> str:
    """A compact thumbnail (base64) small enough to store in Milvus (< 64 KB)."""
    img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    if img is not None:
        jpeg = _encode(_resize_frame(img, max_side), quality)
    return base64.b64encode(jpeg).decode()


def resolve_youtube(url: str) -> str:
    """Download a YouTube video to a temp mp4 (low-res, progressive) with yt-dlp."""
    import yt_dlp
    fd, path = tempfile.mkstemp(suffix=".mp4")
    os.close(fd)
    opts = {
        "format": "best[ext=mp4][height<=480]/best[ext=mp4]/best",
        "outtmpl": path, "overwrites": True, "quiet": True,
        "no_warnings": True, "noplaylist": True,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except Exception as e:  # noqa: BLE001
        if os.path.exists(path):
            os.remove(path)
        raise HTTPException(status_code=400, detail=f"could not fetch YouTube video: {e}")
    return path


def sample_frames(source: str, interval: float, cap_frames: int, max_side: int) -> list[tuple[float, bytes]]:
    """Open a video (file path, http(s) URL or rtsp:// URL) and grab one downscaled
    frame every `interval` seconds, returning (timestamp_seconds, jpeg) samples.
    Capped at `cap_frames`; for long videos the interval is widened so the samples
    still span the whole video instead of only its first minutes."""
    interval = max(0.1, float(interval))
    cap_frames = max(1, min(int(cap_frames), 64))
    vc = cv2.VideoCapture(source)
    if not vc.isOpened():
        raise HTTPException(status_code=400, detail=f"could not open video source: {source}")
    total = int(vc.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    fps = vc.get(cv2.CAP_PROP_FPS) or 25.0
    out: list[tuple[float, bytes]] = []
    if total > 0:
        duration = total / fps
        eff = interval
        if duration / eff > cap_frames:  # too many — widen so we span the whole video
            eff = duration / cap_frames
        t = 0.0
        while t <= duration + 1e-6 and len(out) < cap_frames:
            idx = min(int(t * fps), total - 1)
            vc.set(cv2.CAP_PROP_POS_FRAMES, idx)
            ok, frame = vc.read()
            if ok:
                jpeg = _encode(_resize_frame(frame, max_side))
                if jpeg:
                    out.append((round(t, 2), jpeg))
            t += eff
    else:
        # Stream (e.g. RTSP) with no known length: grab one frame every `interval`
        # seconds of wall-clock, up to the cap.
        last = -1e9
        while len(out) < cap_frames:
            ok, frame = vc.read()
            if not ok:
                break
            now = time.time()
            if now - last < interval:
                continue
            last = now
            jpeg = _encode(_resize_frame(frame, max_side))
            if jpeg:
                out.append((round(len(out) * interval, 2), jpeg))
    vc.release()
    if not out:
        raise HTTPException(status_code=400, detail="no frames could be read from the source")
    return out


# ------------------------------------------------------------------------ Ollama
def ollama_tags() -> set[str]:
    try:
        r = requests.get(f"{OLLAMA_ROOT}/api/tags", timeout=10)
        r.raise_for_status()
        return {m["name"] for m in r.json().get("models", [])}
    except Exception as e:  # noqa: BLE001
        print(f"[ollama] tags failed: {e}", flush=True)
        return set()


def model_supports_vision(name: str) -> Optional[bool]:
    """True/False if Ollama reports the model's capabilities, else None (unknown)."""
    try:
        r = requests.post(f"{OLLAMA_ROOT}/api/show", json={"name": name}, timeout=30)
        r.raise_for_status()
        caps = r.json().get("capabilities") or []
        return ("vision" in caps) if caps else None
    except Exception as e:  # noqa: BLE001
        print(f"[ollama] show failed: {e}", flush=True)
        return None


def ensure_model(name: str) -> None:
    """Ensure a model is present in Ollama, pulling it if not (blocks until done)."""
    tags = ollama_tags()
    base = name.split(":")[0]
    if name in tags or f"{name}:latest" in tags or any(t.split(":")[0] == base for t in tags):
        return
    print(f"[ollama] pulling model {name} …", flush=True)
    r = requests.post(f"{OLLAMA_ROOT}/api/pull", json={"name": name, "stream": False}, timeout=3600)
    if r.status_code >= 400:
        raise HTTPException(status_code=400, detail=f"could not pull model '{name}': {r.text[:200]}")


_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def _strip_think(text: str) -> str:
    return _THINK_RE.sub("", text or "").strip()


def _b64(jpeg: bytes) -> str:
    return base64.b64encode(jpeg).decode()


def caption(jpeg: bytes, prompt: str, model: str) -> str:
    payload = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{_b64(jpeg)}"}},
            ],
        }],
        "stream": False,
        "think": False,
    }
    r = requests.post(f"{OLLAMA_BASE_URL}/chat/completions", json=payload, timeout=600)
    r.raise_for_status()
    return _strip_think(r.json()["choices"][0]["message"]["content"])


def summarize(prompt: str, captions: list[str], model: str) -> str:
    joined = "\n".join(f"- {c}" for c in captions)
    payload = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": (
                "These are per-frame observations sampled in order from a single video:\n"
                f"{joined}\n\n"
                f"Task: {prompt}\n\n"
                "Using ONLY the observations above (do not invent anything not mentioned), "
                "write a concise 3-5 sentence summary of the video that addresses the task. "
                "Note what stays constant and what changes across the frames. If the "
                "observations are insufficient, say so briefly."
            ),
        }],
        "stream": False,
        "think": False,
    }
    r = requests.post(f"{OLLAMA_BASE_URL}/chat/completions", json=payload, timeout=600)
    r.raise_for_status()
    return _strip_think(r.json()["choices"][0]["message"]["content"])


def _index_async(video_id: str, items: list[tuple]) -> None:
    """Compute the CLIP image embedding + a compact thumbnail for each frame and
    store them with metadata in Milvus, in a daemon thread so indexing never delays
    the streamed response. Best-effort."""
    def work():
        col = _milvus()
        if col is None:
            return
        for fid, ts, source, caption_text, jpeg in items:
            vec = clip_image_embed(jpeg)
            if vec is None:
                continue
            try:
                col.insert([{
                    "id": fid, "video_id": video_id, "ts": ts, "source": source,
                    "caption": caption_text, "thumb": _thumb_b64(jpeg), "vector": vec,
                }])
            except Exception as e:  # noqa: BLE001
                print(f"[milvus] insert failed: {e}", flush=True)
        try:
            col.flush()
        except Exception:  # noqa: BLE001
            pass
    threading.Thread(target=work, daemon=True).start()


# ------------------------------------------------------------------------ routes
@app.get("/api/health")
def health():
    return {"ok": True, "vlm_model": VLM_MODEL, "clip_model": CLIP_MODEL,
            "cpus": round(cpu_count_effective(), 1), "auto_frames": auto_frame_count(),
            "auto_interval": auto_interval(), "max_side": auto_max_side()}


@app.get("/api/prompts")
def prompts():
    return {"prompts": PROMPTS}


@app.get("/api/models")
def models():
    return {"default": VLM_MODEL, "installed": sorted(ollama_tags()), "suggested": SUGGESTED_VLMS}


@app.post("/api/pull")
def pull(name: str = Form(...)):
    ensure_model(name.strip())
    return {"ok": True, "name": name.strip(), "installed": sorted(ollama_tags())}


@app.post("/api/analyze")
async def analyze(
    source_type: str = Form(...),
    prompt_id: str = Form("describe"),
    prompt: str = Form(""),
    model: str = Form(""),
    interval: float = Form(0),
    frame_count: int = Form(0),
    source: str = Form(""),
    file: Optional[UploadFile] = File(None),
    frames: list[UploadFile] = File(default=[]),
):
    """Stream the analysis as newline-delimited JSON (status / frame / summary /
    error / done). One frame is sampled every `interval` seconds (auto by CPU when
    0), capped at `frame_count` frames (auto by CPU when 0); resolution also adapts
    to the CPU. A SYNC generator keeps the event loop free (frames flush live)."""
    text_prompt = prompt.strip() or PROMPT_BY_ID.get(prompt_id, PROMPT_BY_ID["describe"])
    vlm = model.strip() or VLM_MODEL
    cpus = cpu_count_effective()
    max_side = auto_max_side()
    step = float(interval) if interval and float(interval) > 0 else auto_interval()
    cap = int(frame_count) if frame_count and int(frame_count) > 0 else auto_frame_count()
    cap = max(1, min(cap, 64))

    # Read request-bound inputs (UploadFiles) up front.
    webcam_frames: list[bytes] = []
    file_bytes: Optional[bytes] = None
    file_name = "upload"
    if source_type == "webcam":
        webcam_frames = [await f.read() for f in frames]
    elif source_type == "upload" and file is not None:
        file_bytes = await file.read()
        file_name = file.filename or "upload"

    def line(obj) -> bytes:
        return (json.dumps(obj) + "\n").encode()

    def gen():
        tmp_path = None
        try:
            yield line({"type": "status", "msg": f"Loading model {vlm}…"})
            ensure_model(vlm)
            if model_supports_vision(vlm) is False:
                yield line({"type": "error", "detail":
                    f"'{vlm}' is a text-only model and can't read video frames. "
                    "Choose a multimodal (vision) model such as moondream:1.8b, "
                    "llava-phi3, llava, gemma3:4b or minicpm-v."}); return

            if source_type == "webcam":
                if not webcam_frames:
                    yield line({"type": "error", "detail": "no webcam frames received"}); return
                frame_list = []
                for i, b in enumerate(webcam_frames):
                    img = cv2.imdecode(np.frombuffer(b, np.uint8), cv2.IMREAD_COLOR)
                    frame_list.append((float(i), _encode(_resize_frame(img, max_side)) if img is not None else b))
                display_source = "webcam"
            elif source_type == "upload":
                if file_bytes is None:
                    yield line({"type": "error", "detail": "no file uploaded"}); return
                fd, tmp_path = tempfile.mkstemp(suffix=Path(file_name).suffix or ".mp4")
                with os.fdopen(fd, "wb") as fh:
                    fh.write(file_bytes)
                frame_list = sample_frames(tmp_path, step, cap, max_side)
                display_source = file_name
            elif source_type == "youtube":
                if not source:
                    yield line({"type": "error", "detail": "missing YouTube URL"}); return
                yield line({"type": "status", "msg": "Downloading YouTube video…"})
                tmp_path = resolve_youtube(source)
                frame_list = sample_frames(tmp_path, step, cap, max_side)
                display_source = source
            elif source_type in ("url", "rtsp"):
                if not source:
                    yield line({"type": "error", "detail": "missing source URL"}); return
                frame_list = sample_frames(source, step, cap, max_side)
                display_source = source
            else:
                yield line({"type": "error", "detail": f"unknown source_type: {source_type}"}); return

            ivl = "1/frame" if source_type == "webcam" else f"1 every {step:g}s"
            yield line({"type": "status",
                        "msg": f"{len(frame_list)} frames ({ivl}, max {cap}) @ {max_side}px (~{round(cpus,1)} CPU); running {vlm}…",
                        "total": len(frame_list)})

            video_id = uuid.uuid4().hex[:12]
            captions: list[str] = []
            to_index: list[tuple] = []
            for i, (ts, jpeg) in enumerate(frame_list):
                try:
                    cap_text = caption(jpeg, text_prompt, vlm)
                except Exception as e:  # noqa: BLE001
                    yield line({"type": "error", "detail": f"model call failed: {e}"}); return
                captions.append(cap_text)
                fid = f"{video_id}-{ts}"
                to_index.append((fid, float(ts), display_source[:1024], cap_text[:4096], jpeg))
                yield line({"type": "frame", "i": i, "ts": ts, "caption": cap_text, "thumb": _b64(jpeg)})

            yield line({"type": "status", "msg": "Summarising…"})
            try:
                summary = summarize(text_prompt, captions, vlm)
            except Exception as e:  # noqa: BLE001
                yield line({"type": "error", "detail": f"summary failed: {e}"}); return
            _index_async(video_id, to_index)  # CLIP embed + Milvus insert in background
            yield line({"type": "summary", "summary": summary, "model": vlm,
                        "indexed": True, "video_id": video_id})
            yield line({"type": "done"})
        except HTTPException as e:
            yield line({"type": "error", "detail": str(e.detail)})
        except Exception as e:  # noqa: BLE001
            yield line({"type": "error", "detail": str(e)})
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.remove(tmp_path)

    return StreamingResponse(
        gen(), media_type="application/x-ndjson",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


@app.post("/api/search")
def search(query: str = Form(...), top_k: int = Form(8)):
    """Text→image semantic search: embed the query with CLIP and match it against
    the stored frame image embeddings in Milvus. Returns frames + metadata."""
    col = _milvus()
    if col is None:
        raise HTTPException(status_code=503, detail="Milvus is not available")
    vec = clip_text_embed(query)
    if vec is None:
        raise HTTPException(status_code=503, detail="CLIP model unavailable")
    res = col.search(
        data=[vec], anns_field="vector", param={"metric_type": "COSINE"},
        limit=max(1, min(int(top_k), 50)),
        output_fields=["video_id", "ts", "source", "caption", "thumb"],
    )
    hits = []
    for hit in res[0]:
        hits.append({
            "score": round(float(hit.distance), 4),
            "video_id": hit.entity.get("video_id"),
            "ts": hit.entity.get("ts"),
            "source": hit.entity.get("source"),
            "caption": hit.entity.get("caption"),
            "thumb": hit.entity.get("thumb"),
        })
    return {"query": query, "hits": hits}


# Static SUSE-styled frontend (mounted last so /api/* wins).
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
