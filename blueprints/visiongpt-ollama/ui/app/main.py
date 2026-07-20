"""
VisionGPT (SUSE) — navigation hazard detection with a local vision-language model.

A SUSE-derivation of AIS-Clemson/VisionGPT ("LLM-Assisted Real-Time Anomaly
Detection for Safe Visual Navigation"). The original used YOLO-World + a text-only
LLM over detection metadata; this version is VLM-native: each sampled video frame
is sent to a locally-served vision-language model over the OpenAI-compatible
/v1/chat/completions API, which returns a per-frame danger score + short reason.

The SAME code drives both blueprint variants — only env differs:
  * Ollama  : OPENAI_BASE_URL=http://localhost:11434/v1  VLM_MODEL=qwen2.5vl:3b
  * vLLM    : OPENAI_BASE_URL=http://localhost:8000/v1   VLM_MODEL=Qwen/Qwen2.5-VL-3B-Instruct

Configuration (env):
  OPENAI_BASE_URL  default http://localhost:11434/v1   (OpenAI-compatible endpoint)
  VLM_MODEL        default qwen2.5vl:3b                 (must match the served model id)
  OPENAI_API_KEY   default EMPTY                        (local servers ignore it)
  FRAME_INTERVAL   default 0 = auto (~1.5s)             (>0 = seconds between sampled frames)
  MAX_FRAMES       default 40                           (cap frames analysed per run)
  SENSITIVITY      default normal                       (low | normal | high)
"""
from __future__ import annotations

import base64
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Optional

import cv2
import requests
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "http://localhost:11434/v1").rstrip("/")
VLM_MODEL = os.environ.get("VLM_MODEL", "qwen2.5vl:3b")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "EMPTY")
FRAME_INTERVAL = float(os.environ.get("FRAME_INTERVAL", "0"))  # 0 = auto
MAX_FRAMES = int(os.environ.get("MAX_FRAMES", "40"))
DEFAULT_SENSITIVITY = os.environ.get("SENSITIVITY", "normal")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
SAMPLES_DIR = Path(__file__).resolve().parent.parent / "samples"
HTTP_TIMEOUT = 120

app = FastAPI(title="VisionGPT (SUSE)")

# --------------------------------------------------------------------------- #
# Prompts (faithful to the VisionGPT paper, adapted for a VLM that sees the frame)
# --------------------------------------------------------------------------- #
SENSITIVITY_PROMPTS = {
    "low": "Report ONLY imminent, direct threats to the person's safety.",
    "normal": "Include potential hazards that could pose a risk if not avoided.",
    "high": "Report anything that could cause any inconvenience or danger; "
    "prioritise pedestrians and vehicles.",
}


def system_prompt(sensitivity: str) -> str:
    tier = SENSITIVITY_PROMPTS.get(sensitivity, SENSITIVITY_PROMPTS["normal"])
    return (
        "You are a navigation assistant for a blind person. You are given a single "
        "frame from a front-facing camera as the person walks forward. Spatial guide: "
        "objects in the left quarter are to the LEFT, the right quarter to the RIGHT, "
        "the upper half is FRONT (farther away), the lower half is GROUND (near the "
        "feet). " + tier + " Judge how dangerous the scene is for walking. "
        'Respond with ONLY a compact JSON object: '
        '{"danger_score": 0 or 1, "reason": "<=10 words"} '
        "where danger_score is 1 for an immediate hazard, else 0."
    )


# --------------------------------------------------------------------------- #
# Frame sampling
# --------------------------------------------------------------------------- #
def auto_interval() -> float:
    return FRAME_INTERVAL if FRAME_INTERVAL > 0 else 1.5


def _resize(frame, max_side: int = 640):
    h, w = frame.shape[:2]
    scale = min(1.0, max_side / max(h, w))
    if scale < 1.0:
        frame = cv2.resize(frame, (int(w * scale), int(h * scale)))
    return frame


def _jpeg_b64(frame, quality: int = 80) -> str:
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise RuntimeError("failed to encode frame")
    return base64.b64encode(buf.tobytes()).decode("ascii")


def sample_frames(path: str):
    """Yield (timestamp_seconds, frame) sampled every `interval` seconds, capped."""
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise RuntimeError(f"could not open video: {path}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    step = max(1, int(fps * auto_interval()))
    idx, taken = 0, 0
    try:
        while taken < MAX_FRAMES:
            ok, frame = cap.read()
            if not ok:
                break
            if idx % step == 0:
                yield idx / fps, _resize(frame)
                taken += 1
            idx += 1
    finally:
        cap.release()


# --------------------------------------------------------------------------- #
# VLM call (OpenAI-compatible; works for both Ollama and vLLM)
# --------------------------------------------------------------------------- #
def analyse_frame(b64: str, sensitivity: str) -> dict:
    payload = {
        "model": VLM_MODEL,
        "temperature": 0,
        "max_tokens": 120,
        "messages": [
            {"role": "system", "content": system_prompt(sensitivity)},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Analyse this frame for navigation hazards."},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                ],
            },
        ],
    }
    r = requests.post(
        f"{OPENAI_BASE_URL}/chat/completions",
        json=payload,
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
        timeout=HTTP_TIMEOUT,
    )
    r.raise_for_status()
    content = r.json()["choices"][0]["message"]["content"]
    return parse_result(content)


def parse_result(text: str) -> dict:
    """Robustly extract {danger_score, reason} from a model reply."""
    text = text.strip()
    # Strip a ```json fence if present.
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    try:
        obj = json.loads(text)
        return {
            "danger_score": 1 if int(obj.get("danger_score", 0)) == 1 else 0,
            "reason": str(obj.get("reason", ""))[:120],
        }
    except Exception:
        pass
    # Fallback: regex for the two fields.
    ds = re.search(r'danger_score"?\s*[:=]\s*("?)([01])\1', text)
    rs = re.search(r'reason"?\s*[:=]\s*"([^"]*)"', text)
    return {
        "danger_score": int(ds.group(2)) if ds else 0,
        "reason": (rs.group(1) if rs else text[:120]),
    }


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.get("/api/health")
def health():
    vlm_ok = False
    try:
        requests.get(f"{OPENAI_BASE_URL}/models", timeout=10).raise_for_status()
        vlm_ok = True
    except Exception:
        pass
    return {"vlm": vlm_ok, "model": VLM_MODEL, "base_url": OPENAI_BASE_URL}


@app.get("/api/models")
def models():
    try:
        r = requests.get(f"{OPENAI_BASE_URL}/models", timeout=10)
        r.raise_for_status()
        names = [m["id"] for m in r.json().get("data", [])]
    except Exception as e:
        raise HTTPException(502, f"VLM endpoint unreachable at {OPENAI_BASE_URL}: {e}")
    return {"models": names, "default": VLM_MODEL}


@app.get("/api/samples")
def samples():
    if not SAMPLES_DIR.exists():
        return {"samples": []}
    vids = sorted(p.name for p in SAMPLES_DIR.glob("*") if p.suffix.lower() in {".mp4", ".mov", ".avi", ".mkv", ".webm"})
    return {"samples": vids}


@app.post("/api/analyze")
async def analyze(
    sample: str = Form(""),
    sensitivity: str = Form(DEFAULT_SENSITIVITY),
    file: Optional[UploadFile] = File(None),
):
    # Resolve the video source to a local path.
    tmp_path = None
    if file is not None:
        suffix = Path(file.filename or "upload.mp4").suffix or ".mp4"
        fd, tmp_path = tempfile.mkstemp(suffix=suffix)
        with os.fdopen(fd, "wb") as f:
            f.write(await file.read())
        path = tmp_path
    elif sample:
        p = (SAMPLES_DIR / sample).resolve()
        if SAMPLES_DIR.resolve() not in p.parents or not p.exists():
            raise HTTPException(400, "unknown sample")
        path = str(p)
    else:
        raise HTTPException(400, "provide a sample name or upload a file")

    def gen():
        hazards = 0
        total = 0
        try:
            for t, frame in sample_frames(path):
                thumb = _jpeg_b64(_resize(frame, 320), quality=70)
                try:
                    res = analyse_frame(_jpeg_b64(frame), sensitivity)
                except Exception as e:  # per-frame failure shouldn't kill the run
                    res = {"danger_score": 0, "reason": f"(error: {e})"}
                total += 1
                hazards += res["danger_score"]
                yield json.dumps({
                    "t": round(t, 2),
                    "thumb": thumb,
                    "danger_score": res["danger_score"],
                    "reason": res["reason"],
                }) + "\n"
            yield json.dumps({"done": True, "frames": total, "hazards": hazards}) + "\n"
        finally:
            if tmp_path:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    return StreamingResponse(gen(), media_type="application/x-ndjson")


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/", StaticFiles(directory=STATIC_DIR), name="static")
