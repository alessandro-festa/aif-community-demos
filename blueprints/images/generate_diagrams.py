#!/usr/bin/env python3
"""
Generate SUSE-branded architecture diagrams for every blueprint.

For each blueprint spec below we emit:
  images/<blueprint>.svg   (self-contained, logos embedded as base64)
  images/<blueprint>.png   (2x raster, via rsvg-convert)

The visual story: everything a blueprint needs runs *inside the SUSE AI Factory
frame on Kubernetes*. The local FastAPI demo UI is drawn dashed/grey OUTSIDE the
frame and labelled "example only - not part of the product".

Run:  python3 generate_diagrams.py
Requires: rsvg-convert on PATH (brew install librsvg).
"""
import base64
import html
import os
import re
import shutil
import struct
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
LOGO_DIR = os.path.join(HERE, "logos")
OUT_DIR = HERE

# ---- SUSE palette -----------------------------------------------------------
JADE = "#0C322C"       # dark green (text / frame)
GREEN = "#30BA78"      # SUSE green (accents / edges)
GREEN_SOFT = "#E8F6EE" # header band
BG = "#FFFFFF"
CARD_BG = "#FFFFFF"
CARD_BORDER = "#CFE3D8"
TEXT = "#16302B"
MUTED = "#5C6B66"
EDGE = "#3E8E6E"
UI_BORDER = "#B4BEB9"
UI_BG = "#F3F5F4"
UI_TEXT = "#6B776F"

# ---- layout constants -------------------------------------------------------
NODE_W = 208
NODE_H = 104
COL_GAP = 104
ROW_GAP = 30
FRAME_PAD = 30
HEADER_H = 66
LANE_H = 26
TITLE_H = 96
OUTER = 34
LOGO_BOX_W = 130
LOGO_BOX_H = 40


# ---- logo handling ----------------------------------------------------------
def _svg_size(data: bytes):
    txt = data.decode("utf-8", "replace")
    m = re.search(r'viewBox\s*=\s*"([\d.\s\-]+)"', txt)
    if m:
        parts = [float(x) for x in m.group(1).split()]
        if len(parts) == 4 and parts[2] > 0 and parts[3] > 0:
            return parts[2], parts[3]
    w = re.search(r'\bwidth\s*=\s*"([\d.]+)', txt)
    h = re.search(r'\bheight\s*=\s*"([\d.]+)', txt)
    if w and h:
        return float(w.group(1)), float(h.group(1))
    return 24.0, 24.0


def _png_size(data: bytes):
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        w, h = struct.unpack(">II", data[16:24])
        return float(w), float(h)
    return 100.0, 100.0


_logo_cache = {}


def logo(name):
    """Return (data_uri, native_w, native_h) or None if no asset on disk."""
    if name in _logo_cache:
        return _logo_cache[name]
    for ext, mime in ((".svg", "image/svg+xml"), (".png", "image/png")):
        p = os.path.join(LOGO_DIR, name + ext)
        if os.path.exists(p):
            with open(p, "rb") as f:
                raw = f.read()
            w, h = (_svg_size(raw) if ext == ".svg" else _png_size(raw))
            uri = "data:%s;base64,%s" % (mime, base64.b64encode(raw).decode())
            res = (uri, w, h)
            _logo_cache[name] = res
            return res
    _logo_cache[name] = None
    return None


def esc(s):
    return html.escape(str(s), quote=True)


# ---- svg primitives ---------------------------------------------------------
def logo_img(name, cx, top, box_w=LOGO_BOX_W, box_h=LOGO_BOX_H):
    info = logo(name)
    if not info:
        return ""
    uri, nw, nh = info
    scale = min(box_w / nw, box_h / nh)
    w, h = nw * scale, nh * scale
    x = cx - w / 2
    y = top + (box_h - h) / 2
    return ('<image href="%s" x="%.1f" y="%.1f" width="%.1f" height="%.1f" '
            'preserveAspectRatio="xMidYMid meet"/>') % (uri, x, y, w, h)


def text_badge(cx, top, label, box_h=LOGO_BOX_H):
    """Fallback 'logo' for products without a usable brand asset."""
    w = max(70, 12 + len(label) * 9.2)
    x = cx - w / 2
    return (
        '<rect x="%.1f" y="%.1f" width="%.1f" height="%d" rx="9" '
        'fill="%s" stroke="%s" stroke-width="1.5"/>'
        '<text x="%.1f" y="%.1f" text-anchor="middle" '
        'font-family="Helvetica,Arial,sans-serif" font-size="16" '
        'font-weight="700" fill="%s">%s</text>'
    ) % (x, top, w, box_h, "#EFF6F2", GREEN, cx, top + box_h / 2 + 5.5,
         JADE, esc(label))


def card(x, y, node):
    cx = x + NODE_W / 2
    parts = [
        '<rect x="%d" y="%d" width="%d" height="%d" rx="14" fill="%s" '
        'stroke="%s" stroke-width="1.5" filter="url(#shadow)"/>'
        % (x, y, NODE_W, NODE_H, CARD_BG, CARD_BORDER)
    ]
    lg = node.get("logo")
    if lg and logo(lg):
        parts.append(logo_img(lg, cx, y + 14))
    else:
        parts.append(text_badge(cx, y + 14, node.get("badge", node["name"])))
    parts.append(
        '<text x="%.1f" y="%d" text-anchor="middle" '
        'font-family="Helvetica,Arial,sans-serif" font-size="15.5" '
        'font-weight="700" fill="%s">%s</text>'
        % (cx, y + 74, TEXT, esc(node["name"]))
    )
    note = node.get("note")
    if note:
        for i, line in enumerate(note.split("\n")[:2]):
            parts.append(
                '<text x="%.1f" y="%d" text-anchor="middle" '
                'font-family="Helvetica,Arial,sans-serif" font-size="11.5" '
                'fill="%s">%s</text>'
                % (cx, y + 90 + i * 13, MUTED, esc(line))
            )
    return "".join(parts)


def ui_card(x, y, node):
    cx = x + NODE_W / 2
    parts = [
        '<rect x="%d" y="%d" width="%d" height="%d" rx="14" fill="%s" '
        'stroke="%s" stroke-width="1.8" stroke-dasharray="7 5"/>'
        % (x, y, NODE_W, NODE_H, UI_BG, UI_BORDER)
    ]
    lg = node.get("logo")
    if lg and logo(lg):
        parts.append(
            '<g opacity="0.55">%s</g>' % logo_img(lg, cx, y + 12, box_h=34))
    parts.append(
        '<text x="%.1f" y="%d" text-anchor="middle" '
        'font-family="Helvetica,Arial,sans-serif" font-size="15" '
        'font-weight="700" fill="%s">%s</text>'
        % (cx, y + 68, UI_TEXT, esc(node["name"])))
    note = node.get("note", "")
    for i, line in enumerate(note.split("\n")[:2]):
        parts.append(
            '<text x="%.1f" y="%d" text-anchor="middle" '
            'font-family="Helvetica,Arial,sans-serif" font-size="11" '
            'fill="%s">%s</text>'
            % (cx, y + 86 + i * 12, UI_TEXT, esc(line)))
    return "".join(parts)


def anchor(box, toward):
    """Border point of box nearest 'toward' center (right/left/top/bottom)."""
    x, y = box["x"], box["y"]
    cx, cy = x + NODE_W / 2, y + NODE_H / 2
    tx, ty = toward
    dx, dy = tx - cx, ty - cy
    if abs(dx) >= abs(dy):
        return (x + NODE_W, cy, "h") if dx > 0 else (x, cy, "h")
    return (cx, y + NODE_H, "v") if dy > 0 else (cx, y, "v")


def _label(mx, my, label, lines=None):
    if not label:
        return ""
    rows = lines if lines else [label]
    w = 8 + max(len(r) for r in rows) * 6.4
    h = 4 + len(rows) * 14
    out = ['<rect x="%.1f" y="%.1f" width="%.1f" height="%d" rx="9" '
           'fill="#FFFFFF" stroke="%s" stroke-width="1" opacity="0.97"/>'
           % (mx - w / 2, my - h / 2, w, h, "#DCE8E2")]
    y0 = my - h / 2 + 14
    for i, r in enumerate(rows):
        out.append('<text x="%.1f" y="%.1f" text-anchor="middle" '
                   'font-family="Helvetica,Arial,sans-serif" font-size="11" '
                   'font-weight="600" fill="%s">%s</text>'
                   % (mx, y0 + i * 13, JADE, esc(r)))
    return "".join(out)


def straight_edge(boxes, src, dst, label):
    a, b = boxes[src], boxes[dst]
    acx, acy = a["x"] + NODE_W / 2, a["y"] + NODE_H / 2
    bcx, bcy = b["x"] + NODE_W / 2, b["y"] + NODE_H / 2
    ax, ay, _ = anchor(a, (bcx, bcy))
    bx, by, _ = anchor(b, (acx, acy))
    mx, my = (ax + bx) / 2, (ay + by) / 2
    line = ('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" '
            'stroke-width="2.2" marker-end="url(#arrow)"/>'
            % (ax, ay, bx, by, EDGE))
    return line + _label(mx, my, label, label.split("\n") if label else None)


def bus_edge(boxes, src, dst, label, busy):
    """Orthogonal route through a clear bottom channel, turning only in the
    column gaps so no card is ever crossed."""
    a, b = boxes[src], boxes[dst]
    acy, bcy = a["y"] + NODE_H / 2, b["y"] + NODE_H / 2
    exit_right = (a["x"] + NODE_W / 2) < (b["x"] + NODE_W / 2)
    if exit_right:
        sx, gsx = a["x"] + NODE_W, a["x"] + NODE_W + COL_GAP / 2
        tx, gtx = b["x"], b["x"] - COL_GAP / 2
    else:
        sx, gsx = a["x"], a["x"] - COL_GAP / 2
        tx, gtx = b["x"] + NODE_W, b["x"] + NODE_W + COL_GAP / 2
    pts = [(sx, acy), (gsx, acy), (gsx, busy), (gtx, busy),
           (gtx, bcy), (tx, bcy)]
    poly = '<polyline points="%s" fill="none" stroke="%s" stroke-width="2.2" ' \
           'marker-end="url(#arrow)"/>' % (
               " ".join("%.1f,%.1f" % p for p in pts), EDGE)
    return poly + _label((gsx + gtx) / 2, busy, label,
                         label.split("\n") if label else None)


# ---- diagram assembly -------------------------------------------------------
def build(spec):
    cols = spec["columns"]
    nodes = spec["nodes"]
    has_ui = "ui" in spec
    ncol = len(cols)
    max_rows = max(len(c["nodes"]) for c in cols)

    # column index of every node (ui sits one column beyond the frame)
    col_of = {}
    for ci, col in enumerate(cols):
        for nid in col["nodes"]:
            col_of[nid] = ci
    col_of["ui"] = ncol

    # an edge that spans >= 2 columns is routed through the bottom channel
    edges = spec.get("edges", [])
    bus_lane = {}
    for i, (s, d, _lbl) in enumerate(edges):
        _ = _lbl
        if abs(col_of.get(s, 0) - col_of.get(d, 0)) >= 2:
            bus_lane[i] = len(bus_lane)
    n_bus = len(bus_lane)

    frame_x = OUTER
    frame_y = TITLE_H
    content_x = frame_x + FRAME_PAD
    content_top = frame_y + HEADER_H + LANE_H + 12
    stack_h = max_rows * NODE_H + (max_rows - 1) * ROW_GAP
    content_bottom = content_top + stack_h
    bus_extra = (24 + n_bus * 24) if n_bus else 0
    frame_h = (content_bottom - frame_y) + bus_extra + FRAME_PAD
    frame_w = 2 * FRAME_PAD + ncol * NODE_W + (ncol - 1) * COL_GAP

    W = frame_x + frame_w + (COL_GAP + NODE_W + OUTER if has_ui else OUTER)
    H = frame_y + frame_h + (52 if has_ui else 24)

    boxes = {}
    for ci, col in enumerate(cols):
        cx = content_x + ci * (NODE_W + COL_GAP)
        n = len(col["nodes"])
        col_h = n * NODE_H + (n - 1) * ROW_GAP
        cy0 = content_top + (stack_h - col_h) / 2
        for ri, nid in enumerate(col["nodes"]):
            boxes[nid] = {"x": cx, "y": cy0 + ri * (NODE_H + ROW_GAP)}

    if has_ui:
        ux = frame_x + frame_w + COL_GAP
        uy = content_top + (stack_h - NODE_H) / 2
        boxes["ui"] = {"x": ux, "y": uy}

    s = []
    s.append(
        '<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" '
        'viewBox="0 0 %d %d" font-family="Helvetica,Arial,sans-serif">'
        % (W, H, W, H))
    s.append('<defs>'
             '<marker id="arrow" markerWidth="10" markerHeight="10" '
             'refX="8" refY="3" orient="auto" markerUnits="userSpaceOnUse">'
             '<path d="M0,0 L9,3 L0,6 Z" fill="%s"/></marker>'
             '<filter id="shadow" x="-20%%" y="-20%%" width="140%%" '
             'height="150%%"><feDropShadow dx="0" dy="2" stdDeviation="3" '
             'flood-color="#0C322C" flood-opacity="0.12"/></filter>'
             '</defs>' % EDGE)
    s.append('<rect width="%d" height="%d" fill="%s"/>' % (W, H, BG))

    # title
    s.append('<text x="%d" y="42" font-size="26" font-weight="800" '
             'fill="%s">%s</text>' % (frame_x, JADE, esc(spec["title"])))
    s.append('<text x="%d" y="70" font-size="14.5" fill="%s">%s</text>'
             % (frame_x, MUTED, esc(spec["subtitle"])))

    # frame + header band
    s.append('<rect x="%d" y="%d" width="%d" height="%d" rx="20" fill="none" '
             'stroke="%s" stroke-width="3"/>'
             % (frame_x, frame_y, frame_w, frame_h, GREEN))
    s.append('<path d="M%d,%d h%d v%d h-%d a20,20 0 0 1 -20,-20 v-%d '
             'a20,20 0 0 1 20,-20 z" fill="%s"/>'
             % (frame_x + 20, frame_y, frame_w - 40, HEADER_H,
                frame_w - 40, HEADER_H - 20, GREEN_SOFT))
    # rounded-top header via full rect clipped by frame radius approximation
    s.append('<rect x="%d" y="%d" width="%d" height="%d" rx="18" fill="%s"/>'
             % (frame_x + 3, frame_y + 3, frame_w - 6, HEADER_H, GREEN_SOFT))
    s.append('<rect x="%d" y="%d" width="%d" height="20" fill="%s"/>'
             % (frame_x + 3, frame_y + HEADER_H - 17, frame_w - 6, GREEN_SOFT))
    # SUSE AI Factory (left)
    s.append(logo_img("suse", frame_x + 30 + 34, frame_y + 14, 68, 38))
    s.append('<text x="%d" y="%d" font-size="18" font-weight="800" fill="%s">'
             'SUSE AI Factory</text>'
             % (frame_x + 78, frame_y + HEADER_H / 2 + 6, JADE))
    # Kubernetes (right)
    kbx = frame_x + frame_w - 30
    s.append('<text x="%d" y="%d" text-anchor="end" font-size="13.5" '
             'font-weight="600" fill="%s">on Kubernetes / Rancher</text>'
             % (kbx - 34, frame_y + HEADER_H / 2 + 5, JADE))
    s.append(logo_img("kubernetes", kbx - 16, frame_y + 16, 30, 34))

    # lane labels
    for ci, col in enumerate(cols):
        cx = content_x + ci * (NODE_W + COL_GAP) + NODE_W / 2
        s.append('<text x="%.1f" y="%d" text-anchor="middle" font-size="12.5" '
                 'font-weight="700" letter-spacing="1.2" fill="%s">%s</text>'
                 % (cx, frame_y + HEADER_H + 20, GREEN,
                    esc(col["label"].upper())))

    # edges first (under cards)
    for i, (src, dst, lbl) in enumerate(edges):
        if src not in boxes or dst not in boxes:
            continue
        if i in bus_lane:
            busy = content_bottom + 24 + bus_lane[i] * 24
            s.append(bus_edge(boxes, src, dst, lbl, busy))
        else:
            s.append(straight_edge(boxes, src, dst, lbl))

    # nodes
    for nid, box in boxes.items():
        if nid == "ui":
            continue
        s.append(card(box["x"], box["y"], nodes[nid]))

    # demo UI outside frame
    if has_ui:
        b = boxes["ui"]
        s.append('<text x="%.1f" y="%d" text-anchor="middle" font-size="12.5" '
                 'font-weight="700" letter-spacing="1.1" fill="%s">%s</text>'
                 % (b["x"] + NODE_W / 2, frame_y + HEADER_H + 20, UI_TEXT,
                    "CONSUMER"))
        s.append(ui_card(b["x"], b["y"], spec["ui"]))
        s.append('<text x="%.1f" y="%.1f" text-anchor="middle" font-size="11" '
                 'font-style="italic" fill="%s">example only - not part</text>'
                 % (b["x"] + NODE_W / 2, b["y"] + NODE_H + 18, UI_TEXT))
        s.append('<text x="%.1f" y="%.1f" text-anchor="middle" font-size="11" '
                 'font-style="italic" fill="%s">of the product</text>'
                 % (b["x"] + NODE_W / 2, b["y"] + NODE_H + 31, UI_TEXT))

    s.append('</svg>')
    return "".join(s)


# ---- shared node fragments --------------------------------------------------
def N(logo_name, name, note=None, badge=None):
    d = {"logo": logo_name, "name": name}
    if note:
        d["note"] = note
    if badge:
        d["badge"] = badge
    return d


UI_FASTAPI = {"logo": "fastapi", "name": "Demo UI", "note": "FastAPI · SUSE-styled"}

# ============================================================================
# Per-blueprint specifications (derived from each README / Blueprint CR)
# ============================================================================
SPECS = {}


def spec(name, **kw):
    SPECS[name] = kw


# --- airflow-genai-rag -------------------------------------------------------
spec("airflow-genai-rag",
    title="Airflow GenAI RAG",
    subtitle="Ingest a knowledge base, build a custom persona model, serve RAG answers",
    nodes={
        "airflow": N("apacheairflow", "Apache Airflow",
                     "ingest_kb · customize_model"),
        "milvus": N("milvus", "Milvus", "vector store · kb"),
        "ollama": N("ollama", "Ollama (CPU)",
                    "llama3.2:1b · nomic-embed\nastra-custom persona"),
    },
    columns=[
        {"label": "Orchestration", "nodes": ["airflow"]},
        {"label": "Vector store", "nodes": ["milvus"]},
        {"label": "Model serving", "nodes": ["ollama"]},
    ],
    ui=UI_FASTAPI,
    edges=[
        ("airflow", "ollama", "embed / create"),
        ("airflow", "milvus", "upsert"),
        ("ui", "ollama", "generate"),
        ("ui", "milvus", "search"),
    ])


# --- data-governance ---------------------------------------------------------
def data_gov(name, serving):
    spec(name,
        title="Data Governance" + (" (vLLM)" if "vllm" in name else " (Ollama)"),
        subtitle="Automated catalog, glossary, lineage & data-quality with a governance copilot",
        nodes={
            "airflow": N("apacheairflow", "Apache Airflow",
                         "seed · lineage · DQ"),
            "om": N("openmetadata", "OpenMetadata", "catalog · glossary\nlineage · policies"),
            "os": N("opensearch", "OpenSearch", "metadata search"),
            "pg": N("postgresql", "PostgreSQL", "OM backend + sample DWH"),
            "llm": serving,
        },
        columns=[
            {"label": "Orchestration", "nodes": ["airflow"]},
            {"label": "Governance platform", "nodes": ["om", "os"]},
            {"label": "Data store", "nodes": ["pg"]},
            {"label": "Copilot LLM", "nodes": ["llm"]},
        ],
        ui=UI_FASTAPI,
        edges=[
            ("airflow", "om", "REST seed"),
            ("airflow", "pg", "compute DQ"),
            ("om", "os", "index"),
            ("om", "pg", "store"),
            ("ui", "om", "discover"),
            ("ui", "llm", "draft / explain"),
        ])


data_gov("data-governance-ollama",
         N("ollama", "Ollama (CPU)", "chosen model"))
data_gov("data-governance-vllm",
         N("vllm", "vLLM (GPU)", "Qwen2.5-1.5B-Instruct"))


# --- dora-compliance ---------------------------------------------------------
def dora(name, serving):
    spec(name,
        title="DORA Compliance" + (" (vLLM)" if "vllm" in name else " (Ollama)"),
        subtitle="EU DORA / BaFin ICT-incident pipeline with an LLM compliance agent",
        nodes={
            "airflow": N("apacheairflow", "Apache Airflow",
                         "simulate · classify\nmarts · index · alerts"),
            "pg": N("postgresql", "PostgreSQL", "incidents · marts"),
            "milvus": N("milvus", "Milvus", "incident embeddings"),
            "llm": serving,
        },
        columns=[
            {"label": "Orchestration", "nodes": ["airflow"]},
            {"label": "Data & vectors", "nodes": ["pg", "milvus"]},
            {"label": "Compliance agent", "nodes": ["llm"]},
        ],
        ui=UI_FASTAPI,
        edges=[
            ("airflow", "pg", "load / marts"),
            ("airflow", "milvus", "index"),
            ("llm", "pg", "tool: query"),
            ("llm", "milvus", "tool: search"),
            ("llm", "airflow", "tool: REST"),
            ("ui", "llm", "explain / ask"),
        ])


dora("dora-compliance-ollama", N("ollama", "Ollama (CPU)", "qwen2.5"))
dora("dora-compliance-vllm",
     N("vllm", "vLLM (GPU)", "Qwen2.5-3B-Instruct\nhermes tool-calls"))


# --- finops-multimodel -------------------------------------------------------
def finops(name, serving):
    spec(name,
        title="FinOps Multi-Model" + (" (vLLM)" if "vllm" in name else " (Ollama)"),
        subtitle="Token spend + infra cost for a guarded, multi-model LLM gateway",
        nodes={
            "airflow": N("apacheairflow", "Apache Airflow",
                         "setup · backfill\ngenerate_traffic"),
            "litellm": N(None, "LiteLLM", "proxy · virtual keys\nspend logs", badge="LiteLLM"),
            "presidio": N(None, "Presidio", "PII guardrail", badge="Presidio"),
            "serve": serving,
            "exporter": N(None, "LiteLLM Exporter", "spend → metrics", badge="LL Exporter"),
            "opencost": N("opencost", "OpenCost", "infra $ / GPU $"),
            "prom": N("prometheus", "Prometheus", "remote-write"),
            "grafana": N("grafana", "Grafana", "FinOps dashboard"),
        },
        columns=[
            {"label": "Orchestration", "nodes": ["airflow"]},
            {"label": "Guarded gateway", "nodes": ["litellm", "presidio"]},
            {"label": "Model serving", "nodes": ["serve"]},
            {"label": "Cost telemetry", "nodes": ["exporter", "opencost"]},
            {"label": "Observability", "nodes": ["prom", "grafana"]},
        ],
        ui=UI_FASTAPI,
        edges=[
            ("airflow", "litellm", "keys / traffic"),
            ("litellm", "presidio", "mask PII"),
            ("litellm", "serve", "route"),
            ("litellm", "exporter", "spend logs"),
            ("exporter", "prom", "metrics"),
            ("opencost", "prom", "cost"),
            ("prom", "grafana", "query"),
            ("ui", "litellm", "chat"),
        ])


finops("finops-multimodel-ollama",
       N("ollama", "Ollama (CPU)", "llama3.2 · qwen2.5\n1.5b / 3b"))
finops("finops-multimodel-vllm",
       N("vllm", "vLLM router (GPU)", "Qwen2.5 0.5/1.5/3B\n3 GPUs"))


# --- fraud-detection ---------------------------------------------------------
def fraud(name, serving):
    spec(name,
        title="Fraud Detection" + (" (vLLM)" if "vllm" in name else " (Ollama)"),
        subtitle="Graph + ML fraud/AML scoring with an LLM investigator",
        nodes={
            "airflow": N("apacheairflow", "Apache Airflow",
                         "gen data · train\nflag + anomaly\nXGBoost · networkx"),
            "pg": N("postgresql", "PostgreSQL", "accounts · scores"),
            "milvus": N("milvus", "Milvus", "behaviour vectors\nKNN anomaly"),
            "llm": serving,
        },
        columns=[
            {"label": "Orchestration + ML", "nodes": ["airflow"]},
            {"label": "Data & vectors", "nodes": ["pg", "milvus"]},
            {"label": "AML analyst", "nodes": ["llm"]},
        ],
        ui=UI_FASTAPI,
        edges=[
            ("airflow", "pg", "write scores"),
            ("airflow", "milvus", "features / KNN"),
            ("ui", "pg", "flagged cases"),
            ("ui", "llm", "explain case"),
        ])


fraud("fraud-detection-ollama", N("ollama", "Ollama (CPU)", "qwen2.5:1.5b"))
fraud("fraud-detection-vllm", N("vllm", "vLLM (GPU)", "Qwen2.5-3B-Instruct"))


# --- insurance-support -------------------------------------------------------
def insurance(name, serving, extra_embed=False):
    nodes = {
        "airflow": N("apacheairflow", "Apache Airflow",
                     "gen dataset · index"),
        "pg": N("postgresql", "PostgreSQL", "customers · claims\ntickets"),
        "milvus": N("milvus", "Milvus", "support_cases"),
        "llm": serving,
    }
    serve_col = ["llm"]
    if extra_embed:
        nodes["embed"] = N("ollama", "Ollama (CPU)", "nomic-embed-text")
        serve_col = ["llm", "embed"]
    spec(name,
        title="Insurance Support" + (" (vLLM)" if "vllm" in name else " (Ollama)"),
        subtitle="Customer-support copilot with text chat, tools & accident-photo vision",
        nodes=nodes,
        columns=[
            {"label": "Orchestration", "nodes": ["airflow"]},
            {"label": "Data & vectors", "nodes": ["pg", "milvus"]},
            {"label": "Model serving", "nodes": serve_col},
        ],
        ui={"logo": "fastapi", "name": "Demo UI",
            "note": "CLIP + Presidio\nFastAPI · example"},
        edges=[
            ("airflow", "pg", "seed"),
            ("airflow", "milvus", "embed cases"),
            ("ui", "llm", "chat / vision"),
            ("ui", "milvus", "semantic search"),
            ("ui", "pg", "tickets"),
        ])


insurance("insurance-support-ollama",
          N("ollama", "Ollama (CPU)", "qwen2.5:3b · vl:3b\nnomic-embed"))
insurance("insurance-support-vllm",
          N("vllm", "vLLM (GPU)", "Qwen2.5-VL-7B"), extra_embed=True)


# --- litellm-guardrails ------------------------------------------------------
spec("litellm-guardrails",
    title="LiteLLM Guardrails",
    subtitle="Guarded, OpenAI-compatible LLM gateway with PII, secret & injection defenses",
    nodes={
        "webui": N("openwebui", "Open WebUI", "chat frontend"),
        "litellm": N(None, "LiteLLM", "OpenAI-compatible\nproxy + Postgres", badge="LiteLLM"),
        "presidio": N(None, "Presidio", "PII mask / block\nsecret redaction", badge="Presidio"),
        "ollama": N("ollama", "Ollama (CPU)", "llama3.2:1b"),
    },
    columns=[
        {"label": "Frontend", "nodes": ["webui"]},
        {"label": "Guarded gateway", "nodes": ["litellm", "presidio"]},
        {"label": "Model serving", "nodes": ["ollama"]},
    ],
    edges=[
        ("webui", "litellm", "request"),
        ("litellm", "presidio", "guardrails"),
        ("litellm", "ollama", "route"),
    ])


# --- suse-vss ----------------------------------------------------------------
spec("suse-vss",
    title="SUSE Video Search & Summarization",
    subtitle="All-SUSE, CPU-only video captioning, summarization and semantic frame search",
    nodes={
        "ollama": N("ollama", "Ollama (CPU)", "moondream:1.8b VLM"),
        "milvus": N("milvus", "Milvus", "CLIP frame vectors\nthumbnails"),
    },
    columns=[
        {"label": "Model serving", "nodes": ["ollama"]},
        {"label": "Vector store", "nodes": ["milvus"]},
    ],
    ui={"logo": "fastapi", "name": "Demo UI",
        "note": "OpenCV + CLIP\nFastAPI · example"},
    edges=[
        ("ui", "ollama", "caption frames"),
        ("ui", "milvus", "embed / search"),
    ])


# --- visiongpt ---------------------------------------------------------------
def visiongpt(name, serving):
    spec(name,
        title="VisionGPT" + (" (vLLM)" if "vllm" in name else " (Ollama)"),
        subtitle="Navigation hazard detection from video frames with a multimodal VLM",
        nodes={"vlm": serving},
        columns=[{"label": "Multimodal serving", "nodes": ["vlm"]}],
        ui={"logo": "fastapi", "name": "Demo UI",
            "note": "frame sampler\nFastAPI · example"},
        edges=[("ui", "vlm", "frame + prompt →\ndanger score")])


visiongpt("visiongpt-ollama", N("ollama", "Ollama (CPU)", "qwen2.5vl:3b"))
visiongpt("visiongpt-vllm", N("vllm", "vLLM (GPU)", "Qwen2.5-VL-3B-Instruct"))


# --- xray-copilot ------------------------------------------------------------
def xray(name, serving):
    spec(name,
        title="X-ray Copilot" + (" (vLLM)" if "vllm" in name else " (Ollama)"),
        subtitle="Chest X-ray analysis & similarity search (demo, not a medical device)",
        nodes={
            "vlm": serving,
            "milvus": N("milvus", "Milvus", "X-ray index"),
        },
        columns=[
            {"label": "Medical VLM serving", "nodes": ["vlm"]},
            {"label": "Vector store", "nodes": ["milvus"]},
        ],
        ui={"logo": "fastapi", "name": "Demo UI",
            "note": "BiomedCLIP\nFastAPI · example"},
        edges=[
            ("ui", "vlm", "analyze image"),
            ("ui", "milvus", "embed / search"),
        ])


xray("xray-copilot-ollama", N("ollama", "Ollama (CPU)", "MedGemma 1.5 4B"))
xray("xray-copilot-vllm",
     N("vllm", "vLLM (GPU)", "LLaVA-Med 7B\n(+ MedGemma opt.)"))


# ---- main -------------------------------------------------------------------
def main():
    if not shutil.which("rsvg-convert"):
        print("ERROR: rsvg-convert not found on PATH", file=sys.stderr)
        sys.exit(1)
    names = sorted(SPECS)
    for name in names:
        svg = build(SPECS[name])
        svg_path = os.path.join(OUT_DIR, name + ".svg")
        png_path = os.path.join(OUT_DIR, name + ".png")
        with open(svg_path, "w") as f:
            f.write(svg)
        subprocess.run(["rsvg-convert", "-z", "2", "-o", png_path, svg_path],
                       check=True)
        print("  %-28s svg+png" % name)
    print("Generated %d diagrams into %s" % (len(names), OUT_DIR))


if __name__ == "__main__":
    main()
