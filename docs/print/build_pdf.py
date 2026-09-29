#!/usr/bin/env python3
"""Build a printable PDF (A4) of docs/09-operating-principles.md with icons and diagrams.

The bullets are read from the Markdown file, so the PDF follows the doc; the icons, diagrams, one-line rules and
layout live here. Standard library only; the PDF is printed by headless Chrome.

    python3 docs/print/build_pdf.py                 # writes docs/print/operating-principles.pdf
    python3 docs/print/build_pdf.py --html out.html  # also keep the intermediate HTML
"""

import argparse
import datetime
import html
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE = os.path.join(HERE, "..", "09-operating-principles.md")
OUTPUT = os.path.join(HERE, "operating-principles.pdf")

CHROME_CANDIDATES = [
    os.environ.get("CHROME", ""),
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "google-chrome",
    "chromium",
    "chromium-browser",
]

# --- Icons: 24×24, stroke-based, drawn with currentColor --------------------------------------------------------

ICONS = {
    "gauge": '<path d="M4 17a8 8 0 1 1 16 0"/><path d="M12 17l4-6"/><circle cx="12" cy="17" r="1.2"/>',
    "users": '<circle cx="9" cy="8" r="3.2"/><path d="M3 20a6 6 0 0 1 12 0"/><circle cx="17" cy="9" r="2.5"/>'
             '<path d="M16 14.2a5 5 0 0 1 5.5 5"/>',
    "clipboard": '<rect x="5" y="4" width="14" height="17" rx="2"/><rect x="9" y="2.5" width="6" height="3.5" rx="1"/>'
                 '<path d="M9 13l2 2 4-4"/>',
    "layers": '<path d="M12 3l9 5-9 5-9-5z"/><path d="M3 12.5l9 5 9-5"/><path d="M3 17l9 5 9-5"/>',
    "cycle": '<path d="M20 12a8 8 0 1 1-2.4-5.7"/><path d="M20 4v5h-5"/>',
    "server": '<rect x="3" y="4" width="18" height="7" rx="2"/><rect x="3" y="13" width="18" height="7" rx="2"/>'
              '<circle cx="7" cy="7.5" r="0.9"/><circle cx="7" cy="16.5" r="0.9"/><path d="M11 7.5h6M11 16.5h6"/>',
    "trend": '<path d="M3 17l6-6 4 4 8-8"/><path d="M15 7h6v6"/>',
    "scale": '<path d="M12 3v18M5 7h14M8 21h8"/><path d="M5 7l-3 7a3 3 0 0 0 6 0z"/><path d="M19 7l-3 7a3 3 0 0 0 6 0z"/>',
    "priority": '<path d="M7 20V4M3 8l4-4 4 4"/><path d="M17 4v16M13 16l4 4 4-4"/>',
    "lock": '<rect x="4" y="10" width="16" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/><path d="M12 14v3"/>',
    "sliders": '<path d="M4 6h16M4 12h16M4 18h16"/><circle cx="9" cy="6" r="2" fill="#fff"/>'
               '<circle cx="15" cy="12" r="2" fill="#fff"/><circle cx="7" cy="18" r="2" fill="#fff"/>',
    "copies": '<rect x="8" y="8" width="13" height="13" rx="2"/><path d="M4 16V5a1 1 0 0 1 1-1h11"/>',
    "shield": '<path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z"/><path d="M8.5 12l2.5 2.5 4.5-4.5"/>',
    "calendar": '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M3 10h18M8 3v4M16 3v4"/>'
                '<path d="M9 15l2 2 4-4"/>',
    "bell": '<path d="M6 16v-5a6 6 0 0 1 12 0v5l2 2H4z"/><path d="M10 21a2 2 0 0 0 4 0"/>',
    "alert": '<path d="M12 3l10 18H2z"/><path d="M12 10v5M12 18v.5"/>',
    "pause": '<circle cx="12" cy="12" r="9"/><path d="M10 9v6M14 9v6"/>',
    "skull": '<path d="M5 11a7 7 0 0 1 14 0v4l-2 1v3H7v-3l-2-1z"/><circle cx="9.5" cy="11.5" r="1.3"/>'
             '<circle cx="14.5" cy="11.5" r="1.3"/><path d="M11 19v-2M13 19v-2"/>',
    "snail": '<path d="M3 18h13a5 5 0 1 0-5-5v5"/><path d="M11 13a2 2 0 1 1 2 2"/><path d="M16 13l3-4M18 14l3-2"/>',
    "check": '<path d="M5 12l5 5 9-10"/>',
    "cross": '<path d="M6 6l12 12M18 6L6 18"/>',
}


def icon(name, size=20, color="currentColor", stroke=1.8):
    return (f'<svg class="ic" width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" stroke="{color}" '
            f'stroke-width="{stroke}" stroke-linecap="round" stroke-linejoin="round">{ICONS[name]}</svg>')


# --- Categories and topics: what the Markdown doesn't hold -----------------------------------------------------

CATEGORIES = {
    "A": dict(name="Capacity", tag="what we can promise", icon="gauge", color="#1d4ed8", tint="#eff6ff",
              question="How much can we sell, and still keep the promise?",
              platform="Owns: sizes clusters, approves quota", teams="Request quota with evidence"),
    "B": dict(name="Tenancy", tag="how we share it", icon="users", color="#6d28d9", tint="#f5f3ff",
              question="How do tenants share a cluster without hurting each other?",
              platform="Sets the rules and enforces them", teams="Stay within their Project"),
    "C": dict(name="Workload standards", tag="what we require from teams", icon="clipboard", color="#0f766e",
              tint="#f0fdfa", question="What must a workload look like to get the guarantees?",
              platform="Defines and checks standards, provides defaults", teams="Implement them in manifests"),
    "D": dict(name="Platform reliability", tag="what we run", icon="layers", color="#b45309", tint="#fffbeb",
              question="Is the platform itself as available as what we promise?",
              platform="Owns end to end", teams="–"),
    "E": dict(name="Day 2 operations", tag="keeping it true over time", icon="cycle", color="#be185d",
              tint="#fdf2f8", question="Do the guarantees stay true after go-live?",
              platform="Runs maintenance, monitoring, reviews", teams="Right-size, fix drift, join reviews"),
}

TOPICS = {
    "A1": ("server", "Σ project quota still fits after the largest node — or failure domain — is gone."),
    "A2": ("gauge", "Sell at most 80–85 % in prod. The rest is reserve, not stock."),
    "A3": ("trend", "Quota covers the maximum (maxReplicas × requests), not the average."),
    "B1": ("scale", "Every project gets what it pays for — quota is the only fairness Kubernetes has."),
    "B2": ("priority", "Platform first, then prod. Priority never bypasses quota."),
    "B3": ("lock", "Quota covers CPU and memory only; guard the rest with limits, policies and node pools."),
    "C1": ("sliders", "Requests are the contract. Prod memory limit = request. CPU limits optional."),
    "C2": ("copies", "At least 2 replicas in prod, each able to carry the load alone."),
    "C3": ("shield", "Spread over nodes, a PDB that allows one disruption, real probes."),
    "D1": ("layers", "3 control plane nodes, 3-node Rancher, 2+ ingress, node VMs on separate hosts."),
    "E1": ("calendar", "Drains succeed unattended; upgrades go one node at a time."),
    "E2": ("bell", "Alert before tenants notice: N+1, quota at 90 %, Pending pods, OOMKills."),
    "E3": ("cycle", "Monthly capacity check, quarterly project review, every quota change via Git."),
}

# --- Markdown (the subset the doc uses) -------------------------------------------------------------------------


def inline(text):
    """Escape and convert inline Markdown: code, links (text only, it's paper), bold, italic."""
    codes = []

    def keep_code(m):
        codes.append(f"<code>{html.escape(m.group(1))}</code>")
        return f"\x00{len(codes) - 1}\x00"

    text = re.sub(r"`([^`]+)`", keep_code, text)
    text = html.escape(text, quote=False)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<i>\1</i>", text)
    return re.sub(r"\x00(\d+)\x00", lambda m: codes[int(m.group(1))], text)


def parse(path):
    """Return {category letter: {"title", "intro", "topics": [{id, title, lead, bullets}]}}."""
    cats, cat, topic, item = {}, None, None, None
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()
    for line in lines:
        m = re.match(r"^## ([A-E])\. (.+)$", line)
        if m:
            cat = cats.setdefault(m.group(1), {"title": m.group(2), "intro": "", "topics": []})
            topic = item = None
            continue
        m = re.match(r"^### ([A-E]\d)\. (.+)$", line)
        if m and cat is not None:
            topic = {"id": m.group(1), "title": m.group(2), "lead": "", "bullets": []}
            cat["topics"].append(topic)
            item = None
            continue
        if cat is None:
            continue
        if line.startswith("## ") or line.strip() == "---":
            item = None
            continue
        if topic is None:
            if line.startswith("*") and line.strip():
                cat["intro"] += " " + line.strip().strip("*")
            elif cat["intro"] and line.strip() and not cat["intro"].endswith("*"):
                cat["intro"] += " " + line.strip().strip("*")
            continue
        if line.startswith("- "):
            item = [line[2:].strip()]
            topic["bullets"].append(item)
        elif line.startswith("  ") and item is not None:
            item.append(line.strip())
        elif line.strip():
            topic["lead"] += " " + line.strip()
            item = None
    for c in cats.values():
        c["intro"] = c["intro"].strip()
        for t in c["topics"]:
            t["bullets"] = [" ".join(b) for b in t["bullets"]]
    return cats


# --- Diagrams (inline SVG, 640 wide) ----------------------------------------------------------------------------

GREY, INK, MUTED, RED = "#94a3b8", "#0f172a", "#475569", "#dc2626"


def svg(h, body, w=640):
    return (f'<svg class="fig" viewBox="0 0 {w} {h}" width="100%" xmlns="http://www.w3.org/2000/svg">'
            f'<defs><pattern id="hatch" width="6" height="6" patternUnits="userSpaceOnUse" '
            f'patternTransform="rotate(45)"><rect width="6" height="6" fill="#fff"/>'
            f'<line x1="0" y1="0" x2="0" y2="6" stroke="{RED}" stroke-width="2.2" opacity=".55"/></pattern>'
            f'<pattern id="hatchg" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">'
            f'<rect width="6" height="6" fill="#fff"/><line x1="0" y1="0" x2="0" y2="6" stroke="{GREY}" '
            f'stroke-width="2.2"/></pattern>'
            f'<marker id="arr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
            f'orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" fill="{MUTED}"/></marker></defs>'
            f'{body}</svg>')


def t(x, y, s, size=11, fill=INK, anchor="start", weight=400):
    return (f'<text x="{x}" y="{y}" font-size="{size}" fill="{fill}" text-anchor="{anchor}" '
            f'font-weight="{weight}">{html.escape(s)}</text>')


def fig_a1():
    c = CATEGORIES["A"]["color"]
    b = t(20, 14, "Example: 4 nodes × 16 CPU", 10, MUTED)
    for i in range(4):
        x = 20 + i * 152
        failed = i == 3
        stroke = RED if failed else c
        b += f'<rect x="{x}" y="22" width="140" height="42" rx="6" fill="#fff" stroke="{stroke}" stroke-width="1.6"/>'
        b += f'<g transform="translate({x + 10},31)" color="{stroke}">{icon("server", 24)}</g>'
        b += t(x + 42, 40, f"Node {i + 1}", 11, INK, weight=600)
        b += t(x + 42, 55, "failed or drained" if failed else "16 CPU", 9, RED if failed else MUTED)
        if failed:
            b += f'<path d="M{x + 116} 34l16 16M{x + 132} 34l-16 16" stroke="{RED}" stroke-width="2.5"/>'
    s = 600 / 64
    room, plat = 44 * s, 4 * s
    y = 92
    b += t(20, 86, "Allocatable 64 CPU", 10, MUTED)
    b += f'<rect x="20" y="{y}" width="{room}" height="30" rx="4" fill="{c}"/>'
    b += f'<rect x="{20 + room}" y="{y}" width="{plat}" height="30" fill="{GREY}"/>'
    b += f'<rect x="{20 + room + plat}" y="{y}" width="{600 - room - plat}" height="30" fill="url(#hatch)" stroke="{RED}"/>'
    b += t(20 + room / 2, y + 20, "Room for projects (N+1): 44 CPU", 12, "#fff", "middle", 700)
    b += t(20 + room + plat / 2, y + 48, "Platform 4", 10, MUTED, "middle")
    b += t(20 + room + plat + 75, y + 20, "Largest node 16", 11, RED, "middle", 700)
    b += t(20, y + 64, "Σ project quota ≤ allocatable − largest node(s) − platform requests", 11, INK, weight=600)
    return svg(166, b)


def fig_a2():
    c = CATEGORIES["A"]["color"]
    x0, full = 120, 300  # 100 % = 300 px
    rows = [("Prod", 0.82, "≤ 80–85 %: guaranteed, N+1"), ("Acc / Test", 1.0, "≤ 100 %: some Pending at peaks"),
            ("Dev", 1.5, "100–150 %: overcommit, not guaranteed")]
    b = ""
    for i, (name, share, note) in enumerate(rows):
        y = 16 + i * 38
        b += t(20, y + 16, name, 12, INK, weight=600)
        b += f'<rect x="{x0}" y="{y}" width="{full}" height="24" rx="4" fill="url(#hatchg)" opacity=".5"/>'
        w = min(share, 1) * full
        b += f'<rect x="{x0}" y="{y}" width="{w}" height="24" rx="4" fill="{c}" opacity="{1 - i * 0.25}"/>'
        if share > 1:
            b += (f'<rect x="{x0 + full}" y="{y}" width="{(share - 1) * full}" height="24" rx="4" '
                  f'fill="#f59e0b"/>')
        b += t(x0 + max(share, 1) * full + 10 if share > 1 else x0 + full + 10, y + 16, note, 10.5, MUTED)
    b += (f'<line x1="{x0 + full}" y1="8" x2="{x0 + full}" y2="130" stroke="{INK}" stroke-width="1.5" '
          f'stroke-dasharray="4 3"/>')
    b += t(x0 + full, 146, "capacity = allocatable − platform (and N+1)", 10, INK, "middle")
    b += (f'<rect x="{x0 + 0.82 * full}" y="16" width="{0.18 * full}" height="24" fill="none" '
          f'stroke="{INK}" stroke-dasharray="2 2"/>')
    b += t(x0 + 0.91 * full, 12, "reserve", 9.5, INK, "middle", 600)
    return svg(154, b)


def fig_a3():
    c = CATEGORIES["A"]["color"]
    b = ""
    for i in range(6):
        x = 30 + i * 96
        solid = i < 2
        dash = "" if solid else 'stroke-dasharray="5 4"'
        b += (f'<rect x="{x}" y="14" width="76" height="44" rx="8" fill="{c if solid else "#fff"}" '
              f'stroke="{c}" stroke-width="1.6" {dash}/>')
        b += t(x + 38, 41, f"pod {i + 1}", 11, "#fff" if solid else c, "middle", 600)
    b += f'<path d="M30 70v8h172v-8" fill="none" stroke="{MUTED}"/>'
    b += t(116, 94, "minReplicas ≥ 2 (prod)", 11, INK, "middle", 600)
    b += f'<path d="M30 104v8h556v-8" fill="none" stroke="{c}" stroke-width="1.6"/>'
    b += t(308, 130, "quota = maxReplicas × requests  (+ rollout surge)", 12, c, "middle", 700)
    return svg(140, b)


def fig_b1():
    c = CATEGORIES["B"]["color"]
    b = t(20, 14, "Tenant = Rancher Project", 11, INK, weight=700)
    b += f'<rect x="20" y="22" width="300" height="128" rx="8" fill="#fff" stroke="{GREY}"/>'
    b += t(30, 38, "Cluster", 10, MUTED)
    for i, (name, q, nss) in enumerate([("Project A", "quota 40 CPU", ["a-api", "a-web"]),
                                        ("Project B", "quota 20 CPU", ["b-api"])]):
        x = 30 + i * 145
        b += f'<rect x="{x}" y="46" width="135" height="94" rx="6" fill="{CATEGORIES["B"]["tint"]}" stroke="{c}"/>'
        b += t(x + 8, 62, name, 11, c, weight=700)
        b += t(x + 8, 76, q, 10, MUTED)
        for j, ns in enumerate(nss):
            b += f'<rect x="{x + 8}" y="{84 + j * 26}" width="119" height="20" rx="4" fill="#fff" stroke="{c}" stroke-dasharray="3 2"/>'
            b += t(x + 16, 98 + j * 26, f"ns {ns}", 10, INK)
    b += t(30, 164, "Platform namespaces: outside quota, deducted first", 10, MUTED)
    # CPU sharing under contention
    b += t(350, 14, "CPU on a busy node: shared by requests", 11, INK, weight=700)
    b += t(350, 44, "requests", 10, MUTED)
    b += f'<rect x="350" y="50" width="160" height="22" rx="4" fill="{c}"/>'
    b += f'<rect x="512" y="50" width="80" height="22" rx="4" fill="#c4b5fd"/>'
    b += t(430, 65, "A: 4 CPU", 10.5, "#fff", "middle", 700) + t(552, 65, "B: 2 CPU", 10.5, INK, "middle", 700)
    b += t(350, 98, "CPU time when all want more", 10, MUTED)
    b += f'<rect x="350" y="104" width="160" height="22" rx="4" fill="{c}"/>'
    b += f'<rect x="512" y="104" width="80" height="22" rx="4" fill="#c4b5fd"/>'
    b += t(430, 119, "⅔", 12, "#fff", "middle", 700) + t(552, 119, "⅓", 12, INK, "middle", 700)
    b += t(350, 148, "Idle CPU may be used by anyone;", 10, MUTED)
    b += t(350, 162, "requested CPU is never taken away.", 10, MUTED)
    return svg(172, b)


def fig_c1():
    c = CATEGORIES["C"]["color"]
    x0 = 150
    b = ""
    rows = [("Memory, prod", 200, 200, "limit = request"),
            ("Memory, non-prod", 150, 300, "limit ≤ 2× request → eviction risk"),
            ("CPU", 150, None, "no limit: may burst into idle CPU")]
    for i, (name, req, lim, note) in enumerate(rows):
        y = 18 + i * 44
        b += t(20, y + 16, name, 12, INK, weight=600)
        b += f'<rect x="{x0}" y="{y}" width="{req}" height="22" rx="4" fill="{c}"/>'
        b += t(x0 + 8, y + 15, "request", 10, "#fff", weight=700)
        if lim is None:
            b += (f'<rect x="{x0 + req}" y="{y}" width="200" height="22" rx="4" fill="none" stroke="{c}" '
                  f'stroke-dasharray="5 4"/>')
            b += f'<line x1="{x0 + req + 10}" y1="{y + 11}" x2="{x0 + req + 190}" y2="{y + 11}" stroke="{c}" marker-end="url(#arr)"/>'
            b += t(x0 + req + 210, y + 15, note, 10.5, MUTED)
        else:
            if lim > req:
                b += f'<rect x="{x0 + req}" y="{y}" width="{lim - req}" height="22" fill="url(#hatch)"/>'
            b += f'<line x1="{x0 + lim}" y1="{y - 5}" x2="{x0 + lim}" y2="{y + 27}" stroke="{RED}" stroke-width="2.5"/>'
            b += t(x0 + lim + 8, y + 15, note, 10.5, MUTED)
    b += t(20, 152, "Quota and cost count requests. Memory above request is borrowed — and evicted first.", 10.5,
           INK, weight=600)
    return svg(160, b)


def fig_c2():
    c = CATEGORIES["C"]["color"]

    def pod(x, level, down=False):
        s = f'<rect x="{x}" y="30" width="70" height="80" rx="8" fill="#fff" stroke="{RED if down else c}" stroke-width="1.6"/>'
        if down:
            s += f'<path d="M{x + 15} 50l40 40M{x + 55} 50l-40 40" stroke="{RED}" stroke-width="2.5"/>'
        else:
            h = 76 * level
            s += f'<rect x="{x + 2}" y="{108 - h}" width="66" height="{h}" rx="6" fill="{c}" opacity=".85"/>'
            s += t(x + 35, 104 - h + (14 if level < 1 else 16), f"{int(level * 100)} %", 11, "#fff", "middle", 700)
        return s

    b = t(40, 18, "Normal: load shared", 11, INK, weight=700)
    b += pod(40, 0.5) + pod(130, 0.5)
    b += f'<line x1="245" y1="70" x2="345" y2="70" stroke="{MUTED}" stroke-width="1.5" marker-end="url(#arr)"/>'
    b += t(295, 60, "drain / failure", 10, MUTED, "middle")
    b += t(370, 18, "One pod gone: the other carries it", 11, INK, weight=700)
    b += pod(370, 1.0) + pod(460, 0, down=True)
    b += t(20, 134, "Size requests so N−1 replicas carry the full load.", 10.5, INK, weight=600)
    return svg(142, b)


def fig_c3():
    c = CATEGORIES["C"]["color"]

    def panel(x0, placement, ok, title):
        s = f'<g transform="translate({x0 + 2},2)" color="{c if ok else RED}">{icon("check" if ok else "cross", 18, stroke=2.6)}</g>'
        s += t(x0 + 26, 16, title, 11, INK, weight=700)
        for n in range(3):
            x = x0 + n * 98
            s += f'<rect x="{x}" y="26" width="88" height="76" rx="6" fill="#fff" stroke="{GREY}"/>'
            s += t(x + 44, 118, f"node {n + 1}", 10, MUTED, "middle")
            for k, p in enumerate(placement.get(n, [])):
                s += (f'<rect x="{x + 10}" y="{34 + k * 32}" width="68" height="26" rx="5" '
                      f'fill="{c if ok else "#fca5a5"}"/>')
                s += t(x + 44, 51 + k * 32, p, 10.5, "#fff" if ok else INK, "middle", 700)
        return s

    b = panel(0, {0: ["pod 1", "pod 2"]}, False, "Two replicas on one node = one replica")
    b += panel(330, {0: ["pod 1"], 1: ["pod 2"], 2: ["pod 3"]}, True, "Spread: topologySpreadConstraints")
    return svg(126, b, w=640)


def fig_d1():
    c = CATEGORIES["D"]["color"]
    tint = CATEGORIES["D"]["tint"]

    def box(x, y, w, h, title, sub, strong=False):
        s = f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="7" fill="{c if strong else tint}" stroke="{c}"/>'
        s += t(x + w / 2, y + 20, title, 11, "#fff" if strong else INK, "middle", 700)
        s += t(x + w / 2, y + 35, sub, 10, "#fff" if strong else MUTED, "middle")
        return s

    b = box(180, 4, 280, 44, "Rancher local cluster", "3 nodes · management, quota changes", True)
    b += f'<line x1="320" y1="48" x2="320" y2="68" stroke="{MUTED}" stroke-width="1.5" marker-end="url(#arr)"/>'
    b += f'<rect x="10" y="72" width="620" height="178" rx="10" fill="#fff" stroke="{GREY}"/>'
    b += t(22, 90, "Downstream cluster", 10.5, MUTED, weight=600)
    b += box(22, 98, 190, 46, "Control plane", "3 etcd nodes (odd), no workloads")
    b += box(225, 98, 190, 46, "Ingress controller", "2+ replicas, spread, own PDB")
    b += box(428, 98, 190, 46, "Platform components", "platform priority, outside quota")
    for i in range(3):
        x = 22 + i * 203
        b += f'<rect x="{x}" y="158" width="190" height="80" rx="7" fill="#fff" stroke="{c}" stroke-dasharray="5 3"/>'
        b += t(x + 10, 174, f"Hypervisor host / rack {i + 1}", 10, c, weight=700)
        b += f'<rect x="{x + 10}" y="182" width="170" height="46" rx="5" fill="{tint}" stroke="{c}"/>'
        b += f'<g transform="translate({x + 18},193)" color="{c}">{icon("server", 22)}</g>'
        b += t(x + 48, 202, f"worker node {i + 1}", 10.5, INK, weight=600)
        b += t(x + 48, 217, "zone label = host / rack", 9.5, MUTED)
    b += t(320, 266, "etcd snapshots and Rancher backups: stored off-cluster", 10.5, INK, "middle", 600)
    return svg(274, b)


def fig_e1():
    c = CATEGORIES["E"]["color"]
    steps = ["Cordon", "Drain", "Upgrade", "Uncordon", "Next node"]
    b = ""
    for i, s in enumerate(steps):
        x = 10 + i * 124
        b += (f'<path d="M{x} 14h104l14 22-14 22h-104l14-22z" fill="{c if i < 4 else "#fff"}" '
              f'stroke="{c}" stroke-width="1.4" opacity="{1 - i * 0.12 if i < 4 else 1}"/>')
        b += t(x + 60, 40, s, 11.5, "#fff" if i < 4 else c, "middle", 700)
    b += t(10, 78, "Don't start when the cluster's N+1 tile is red.", 10.5, RED, weight=600)
    return svg(84, b)


def fig_e3():
    c = CATEGORIES["E"]["color"]
    steps = [("VPA / KRR", "recommendations"), ("Team adjusts", "requests"), ("Quota change", "via Git PR"),
             ("Quarterly", "review")]
    b = ""
    for i, (a, s) in enumerate(steps):
        x = 14 + i * 158
        b += f'<rect x="{x}" y="10" width="128" height="48" rx="24" fill="{CATEGORIES["E"]["tint"]}" stroke="{c}" stroke-width="1.5"/>'
        b += t(x + 64, 30, a, 11, INK, "middle", 700) + t(x + 64, 46, s, 10, MUTED, "middle")
        if i < 3:
            b += f'<line x1="{x + 130}" y1="34" x2="{x + 154}" y2="34" stroke="{MUTED}" stroke-width="1.5" marker-end="url(#arr)"/>'
    b += (f'<path d="M552 58v18H78v-14" fill="none" stroke="{MUTED}" stroke-width="1.5" '
          f'stroke-dasharray="4 3" marker-end="url(#arr)"/>')
    b += t(315, 92, "repeat every quarter · monthly look at allocation ratio and N+1 per cluster", 10.5, MUTED, "middle")
    return svg(100, b)


FIGURES = {"A1": fig_a1, "A2": fig_a2, "A3": fig_a3, "B1": fig_b1, "C1": fig_c1, "C2": fig_c2, "C3": fig_c3,
           "D1": fig_d1, "E1": fig_e1, "E3": fig_e3}

ALERT_ICONS = ["server", "gauge", "pause", "skull", "snail", "alert"]


def fig_overview():
    """Cover diagram: D underpins A, A enables B and C; E keeps all of them true."""
    b = ""
    order = ["D", "A", "B", "C"]
    for i, k in enumerate(order):
        cat = CATEGORIES[k]
        x = 8 + i * 158
        b += f'<rect x="{x}" y="8" width="136" height="70" rx="10" fill="{cat["tint"]}" stroke="{cat["color"]}" stroke-width="1.6"/>'
        b += f'<g transform="translate({x + 56},16)" color="{cat["color"]}">{icon(cat["icon"], 24)}</g>'
        b += t(x + 68, 56, f'{k}. {cat["name"]}', 11, cat["color"], "middle", 700)
        b += t(x + 68, 70, cat["tag"], 9.5, MUTED, "middle")
        if i < 3:
            b += f'<line x1="{x + 138}" y1="43" x2="{x + 156}" y2="43" stroke="{MUTED}" stroke-width="1.6" marker-end="url(#arr)"/>'
    e = CATEGORIES["E"]
    b += f'<rect x="8" y="112" width="610" height="42" rx="10" fill="{e["tint"]}" stroke="{e["color"]}" stroke-width="1.6"/>'
    b += f'<g transform="translate(200,121)" color="{e["color"]}">{icon(e["icon"], 24)}</g>'
    b += t(232, 138, "E. Day 2 operations — keeps all of it true", 11.5, e["color"], weight=700)
    for i in range(4):
        x = 76 + i * 158
        b += f'<line x1="{x}" y1="110" x2="{x}" y2="82" stroke="{e["color"]}" stroke-width="1.4" stroke-dasharray="4 3" marker-end="url(#arr)"/>'
    return svg(160, b, w=626)


# --- Page ---------------------------------------------------------------------------------------------------------

CSS = """
@page { size: A4; margin: 14mm 15mm 16mm 15mm;
  @bottom-left { content: "Operating Principles · Cluster resource allocation"; font: 8pt system-ui, sans-serif; color: #64748b; }
  @bottom-right { content: counter(page) " / " counter(pages); font: 8pt system-ui, sans-serif; color: #64748b; } }
@page cover { @bottom-left { content: none; } @bottom-right { content: none; } }
* { box-sizing: border-box; -webkit-print-color-adjust: exact; print-color-adjust: exact; }
html { font: 9.2pt/1.36 -apple-system, "Segoe UI", "Helvetica Neue", Arial, sans-serif; color: #0f172a; }
body { margin: 0; background: #fff; }
code { font: 8.6pt ui-monospace, "SF Mono", Menlo, Consolas, monospace; background: #f1f5f9; padding: 0 3px; border-radius: 3px; }
b { font-weight: 650; }
svg text { font-family: -apple-system, "Segoe UI", "Helvetica Neue", Arial, sans-serif; }
.ic { vertical-align: middle; flex: none; }
.cover { page: cover; height: 265mm; display: flex; flex-direction: column; }
.cover .kicker { font-size: 9pt; letter-spacing: .14em; text-transform: uppercase; color: #64748b; margin-top: 18mm; }
.cover h1 { font-size: 34pt; line-height: 1.05; margin: 4mm 0 3mm; letter-spacing: -.02em; }
.cover .sub { font-size: 12pt; color: #334155; max-width: 150mm; }
.cover .band { display: flex; gap: 2mm; margin: 9mm 0 8mm; }
.cover .band span { flex: 1; height: 3mm; border-radius: 2mm; }
.cover .fig { margin: 2mm 0 6mm; }
.cards { display: grid; grid-template-columns: repeat(5, 1fr); gap: 3mm; }
.card { border: 1px solid; border-radius: 3mm; padding: 3mm; font-size: 8.4pt; line-height: 1.35; }
.card h3 { margin: 1.5mm 0 1.5mm; font-size: 10pt; }
.card .q { color: #334155; margin-bottom: 2mm; }
.card .role { font-size: 7.6pt; color: #475569; }
.card .role b { color: #0f172a; }
.cover .meta { margin-top: auto; font-size: 8.5pt; color: #64748b; display: flex; justify-content: space-between; border-top: 1px solid #e2e8f0; padding-top: 3mm; }
h2.page-title { font-size: 18pt; margin: 0 0 1mm; letter-spacing: -.01em; }
.lede { color: #475569; margin: 0 0 5mm; }
table.glance { width: 100%; border-collapse: collapse; }
table.glance td { padding: 2.2mm 2mm; border-bottom: 1px solid #e2e8f0; vertical-align: middle; }
table.glance tr.cat td { padding-top: 4mm; border-bottom: 2px solid; font-weight: 700; font-size: 10.5pt; }
table.glance td.id { width: 11mm; font-weight: 700; }
table.glance td.ic { width: 9mm; }
table.glance td.name { width: 44mm; font-weight: 600; }
table.glance td.box { width: 7mm; }
table.glance .tick { display: inline-block; width: 4mm; height: 4mm; border: 1.2px solid #94a3b8; border-radius: 1mm; }
section.cat { break-before: page; }
.keep { break-inside: avoid; margin-bottom: 2mm; }
section.cat.flow { break-before: auto; margin-top: 5mm; }
.banner { display: flex; gap: 4mm; align-items: center; border-radius: 3mm; padding: 3mm 5mm; margin-bottom: 3mm; color: #fff; }
.banner .big { width: 15mm; height: 15mm; border-radius: 50%; background: rgba(255,255,255,.18); display: flex; align-items: center; justify-content: center; }
.banner h2 { margin: 0; font-size: 17pt; letter-spacing: -.01em; }
.banner .tag { font-size: 10pt; opacity: .92; }
.intro { display: flex; gap: 4mm; margin-bottom: 3mm; align-items: stretch; }
.intro p { margin: 0; flex: 1.4; color: #334155; font-style: italic; }
.chip { flex: 1; border-radius: 2mm; padding: 2mm 3mm; font-size: 8.4pt; border: 1px solid; }
.chip small { display: block; text-transform: uppercase; letter-spacing: .08em; font-size: 6.8pt; font-weight: 700; }
.topic { margin: 0 0 3.5mm; }
.topic-head { display: flex; align-items: center; gap: 3mm; break-after: avoid; }
.topic-head .dot { width: 9mm; height: 9mm; border-radius: 50%; display: flex; align-items: center; justify-content: center; color: #fff; }
.topic-head h3 { margin: 0; font-size: 12.5pt; }
.topic-head .id { font-weight: 800; margin-right: 1.5mm; }
.rule { margin: 1.5mm 0 2mm; padding: 2mm 3mm; border-left: 1.2mm solid; border-radius: 0 2mm 2mm 0; font-weight: 600; break-after: avoid; }
.figure { border: 1px solid #e2e8f0; border-radius: 2.5mm; padding: 1.5mm 3mm 0.5mm; margin: 0 0 2mm; break-inside: avoid; text-align: center; }
.figure svg { width: 88%; }
.figure.D1 svg { width: 70%; }  /* tall: smaller, so E can start on D's page */
.topic ul { margin: 0; padding-left: 4.5mm; }
.topic li { margin: 0 0 0.6mm; break-inside: avoid; }
.topic li::marker { font-size: 8pt; }
.lead { margin: 0 0 2mm; color: #334155; }
.alerts { display: grid; grid-template-columns: repeat(3, 1fr); gap: 1.8mm; }
.alert { display: flex; gap: 2mm; align-items: flex-start; border: 1px solid; border-radius: 2mm; padding: 1.5mm 2mm; font-size: 8.8pt; break-inside: avoid; }
"""


def render(cats):
    today = datetime.date.today().strftime("%d %B %Y").lstrip("0")
    out = [f"<!doctype html><html lang='en'><head><meta charset='utf-8'><title>Operating Principles</title>"
           f"<style>{CSS}</style></head><body>"]

    # Cover
    band = "".join(f'<span style="background:{CATEGORIES[k]["color"]}"></span>' for k in "ABCDE")
    cards = ""
    for k in "ABCDE":
        c = CATEGORIES[k]
        topics = " · ".join(f"{tid} {cats[k]['topics'][i]['title'].split(' (')[0]}"
                            for i, tid in enumerate(t_["id"] for t_ in cats[k]["topics"]))
        cards += (f'<div class="card" style="border-color:{c["color"]};background:{c["tint"]}">'
                  f'<span style="color:{c["color"]}">{icon(c["icon"], 22)}</span>'
                  f'<h3 style="color:{c["color"]}">{k}. {html.escape(c["name"])}</h3>'
                  f'<div class="q">{html.escape(c["question"])}</div>'
                  f'<div class="role"><b>Platform:</b> {html.escape(c["platform"])}</div>'
                  f'<div class="role"><b>Teams:</b> {html.escape(c["teams"])}</div>'
                  f'<div class="role" style="margin-top:2mm;color:{c["color"]}">{html.escape(topics)}</div></div>')
    out.append(f'<div class="cover"><div class="kicker">Cluster resource allocation · Platform team</div>'
               f'<h1>Operating<br>Principles</h1>'
               f'<div class="sub">The rules behind the allocation model — N+1, fairness between tenants, conservative '
               f'allocation, requests and limits, replicas, HA and day 2 operations — grouped by what the platform '
               f'team owns, enforces, requires and runs.</div>'
               f'<div class="band">{band}</div>{fig_overview()}<div class="cards">{cards}</div>'
               f'<div class="meta"><span>Source: docs/09-operating-principles.md</span><span>Printed {today}</span>'
               f'</div></div>')

    # At a glance
    rows = ""
    for k in "ABCDE":
        c = CATEGORIES[k]
        rows += (f'<tr class="cat" style="color:{c["color"]};border-color:{c["color"]}"><td colspan="5" '
                 f'style="border-color:{c["color"]}">{k}. {html.escape(c["name"])} — {html.escape(c["tag"])}</td></tr>')
        for tp in cats[k]["topics"]:
            ic, rule = TOPICS[tp["id"]]
            rows += (f'<tr><td class="ic" style="color:{c["color"]}">{icon(ic, 18)}</td>'
                     f'<td class="id" style="color:{c["color"]}">{tp["id"]}</td>'
                     f'<td class="name">{inline(tp["title"])}</td><td>{html.escape(rule)}</td>'
                     f'<td class="box"><span class="tick"></span></td></tr>')
    out.append(f'<section class="cat"><h2 class="page-title">At a glance</h2>'
               f'<p class="lede">One rule per topic. The box is for reviews: tick what a cluster or project meets '
               f'today.</p><table class="glance">{rows}</table></section>')

    # Categories
    for k in "ABCDE":
        c = CATEGORIES[k]
        cat = cats[k]
        # D is half a page: E follows it on the same page instead of leaving it half empty.
        flow = " flow" if k == "E" else ""
        # The banner stays on the same page as the first topic's heading, rule and figure (class "keep").
        out.append(f'<section class="cat{flow}"><div class="keep"><div class="banner" style="background:{c["color"]}">'
                   f'<div class="big">{icon(c["icon"], 32, "#fff")}</div><div>'
                   f'<h2>{k}. {html.escape(c["name"])}</h2><div class="tag">{html.escape(c["tag"])} · '
                   f'{html.escape(c["question"])}</div></div></div>'
                   f'<div class="intro"><p>{inline(cat["intro"])}</p>'
                   f'<div class="chip" style="border-color:{c["color"]};background:{c["tint"]}">'
                   f'<small style="color:{c["color"]}">Platform team</small>{html.escape(c["platform"])}</div>'
                   f'<div class="chip" style="border-color:#cbd5e1"><small style="color:#475569">Teams</small>'
                   f'{html.escape(c["teams"])}</div></div>')
        for n, tp in enumerate(cat["topics"]):
            ic, rule = TOPICS[tp["id"]]
            out.append(f'{"<div class=topic>" if n else ""}<div class="topic-head"><div class="dot" style="background:{c["color"]}">'
                       f'{icon(ic, 19, "#fff")}</div><h3><span class="id" style="color:{c["color"]}">{tp["id"]}</span>'
                       f'{inline(tp["title"])}</h3></div>'
                       f'<div class="rule" style="border-color:{c["color"]};background:{c["tint"]}">{html.escape(rule)}</div>')
            if tp["id"] in FIGURES:
                out.append(f'<div class="figure {tp["id"]}">{FIGURES[tp["id"]]()}</div>')
            if n == 0:
                out.append("</div>")  # closes "keep"
            if tp["lead"].strip():
                out.append(f'<p class="lead">{inline(tp["lead"].strip())}</p>')
            if tp["id"] == "E2":
                cards = "".join(
                    f'<div class="alert" style="border-color:{c["color"]}"><span style="color:{c["color"]}">'
                    f'{icon(ALERT_ICONS[i % len(ALERT_ICONS)], 20)}</span><span>{inline(bl)}</span></div>'
                    for i, bl in enumerate(tp["bullets"]))
                out.append(f'<div class="alerts">{cards}</div>')
            else:
                out.append("<ul>" + "".join(f"<li>{inline(bl)}</li>" for bl in tp["bullets"]) + "</ul>")
            out.append("</div>" if n else "")
        out.append("</section>")
    out.append("</body></html>")
    return "".join(out)


def find_chrome():
    for c in CHROME_CANDIDATES:
        if c and (os.path.exists(c) or shutil.which(c)):
            return c if os.path.exists(c) else shutil.which(c)
    sys.exit("Chrome/Chromium not found; set CHROME=/path/to/chrome")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=OUTPUT, help="PDF to write (default: %(default)s)")
    ap.add_argument("--html", help="also write the intermediate HTML here")
    args = ap.parse_args()

    cats = parse(SOURCE)
    missing = [tp["id"] for c in cats.values() for tp in c["topics"] if tp["id"] not in TOPICS]
    if set(cats) != set(CATEGORIES) or missing:
        sys.exit(f"Doc structure changed: categories {sorted(cats)}, topics without a rule/icon {missing}")
    page = render(cats)

    with tempfile.TemporaryDirectory() as tmp:
        src = args.html or os.path.join(tmp, "operating-principles.html")
        with open(src, "w", encoding="utf-8") as f:
            f.write(page)
        out = os.path.abspath(args.out)
        if os.path.exists(out):
            os.remove(out)
        # Chrome on macOS sometimes keeps running after printing: wait for the PDF, then stop it.
        proc = subprocess.Popen([find_chrome(), "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
                                 "--no-first-run", "--no-default-browser-check", "--disable-extensions",
                                 f"--user-data-dir={os.path.join(tmp, 'profile')}",
                                 f"--print-to-pdf={out}", "file://" + os.path.abspath(src)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        size, deadline = -1, time.time() + 90
        while time.time() < deadline and proc.poll() is None:
            time.sleep(1)
            now = os.path.getsize(out) if os.path.exists(out) else -1
            if now > 0 and now == size:
                break
            size = now
        if proc.poll() is None:
            proc.terminate()
            proc.wait(10)
        if not os.path.exists(out) or os.path.getsize(out) == 0:
            sys.exit("Chrome didn't write the PDF")
    print(args.out)


if __name__ == "__main__":
    main()
