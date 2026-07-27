"""
VisionGPT (SUSE) — navigation hazard detection with a local vision-language model.

A SUSE-derivation of AIS-Clemson/VisionGPT ("LLM-Assisted Real-Time Anomaly
Detection for Safe Visual Navigation"). The original used YOLO-World + a text-only
LLM over detection metadata; this version is VLM-native: each sampled video frame
is sent to a locally-served vision-language model over the OpenAI-compatible
/v1/chat/completions API, which returns a per-frame danger score + short reason.

This Ollama (CPU) variant is tuned for speed and serves moondream:1.8b — a much
smaller, simpler VLM than the vLLM variant's Qwen2.5-VL-3B-Instruct. moondream is
not a strong instruction-follower for a JSON-schema system prompt, so this variant
asks a direct yes/no hazard question instead (see hazard_question / parse_result
below) — the vLLM variant keeps the original JSON-object prompt, which Qwen2.5-VL
follows reliably.

Configuration (env):
  OPENAI_BASE_URL  default http://localhost:11434/v1   (OpenAI-compatible endpoint)
  VLM_MODEL        default moondream:1.8b               (must match the served model id)
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
VLM_MODEL = os.environ.get("VLM_MODEL", "moondream:1.8b")
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
# moondream is a small, direct-answer VQA model — it doesn't reliably follow a
# system-prompt persona + JSON-schema instruction the way Qwen2.5-VL does. So
# this variant asks a single, direct yes/no question instead of requesting
# structured JSON; parse_result() below parses that plain-text answer.
SENSITIVITY_PROMPTS = {
    "low": "Only count imminent, direct threats to the person's safety.",
    "normal": "Include potential hazards that could pose a risk if not avoided.",
    "high": "Count anything that could cause any inconvenience or danger, "
    "especially pedestrians and vehicles.",
}


def hazard_question(sensitivity: str) -> str:
    tier = SENSITIVITY_PROMPTS.get(sensitivity, SENSITIVITY_PROMPTS["normal"])
    return (
        "This photo is one frame from a blind person's front-facing walking camera. "
        + tier + " Is there a hazard in this frame that is dangerous for someone "
        "walking forward? Answer with exactly one line: start with YES or NO, then a "
        "colon, then the hazard in 6 words or fewer (or 'clear' if NO)."
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
    # A single user turn (question + image) — moondream doesn't need or reliably
    # honour a separate system-role persona, so everything goes in one message.
    payload = {
        "model": VLM_MODEL,
        "temperature": 0,
        "max_tokens": 40,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": hazard_question(sensitivity)},
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
    """Robustly extract {danger_score, reason} from a model reply.

    Primary path: moondream's plain "YES: <reason>" / "NO: <reason>" answer.
    Fallback: a JSON object {"danger_score": 0|1, "reason": "..."} — kept for
    compatibility if VLM_MODEL is ever pointed back at a JSON-following VLM
    (e.g. Qwen2.5-VL, as in the vLLM variant of this blueprint).
    """
    text = text.strip()
    m = re.match(r"^(yes|no)\b[:\-,]?\s*(.*)$", text, re.IGNORECASE | re.DOTALL)
    if m:
        reason = m.group(2).strip().splitlines()[0] if m.group(2).strip() else text
        return {
            "danger_score": 1 if m.group(1).lower() == "yes" else 0,
            "reason": reason[:120],
        }
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
    # Last resort: regex for the two JSON-shaped fields.
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
