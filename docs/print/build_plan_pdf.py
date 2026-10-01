#!/usr/bin/env python3
"""Build a printable PDF (A4) of docs/10-implementation-plan.md with phase banners, step cards and diagrams.

The text, tables and steps are read from the Markdown file, so the PDF follows the doc; the colours, icons and the
drawings that replace the Mermaid diagrams live here (the timeline is drawn from the doc's gantt block). The
Markdown doc is the source of truth: change the plan there, not here. Standard library only; the PDF is printed by
headless Chrome, as for the operating principles (build_pdf.py).

    python3 docs/print/build_plan_pdf.py                 # writes docs/print/implementation-plan.pdf
    python3 docs/print/build_plan_pdf.py --html out.html  # also keep the intermediate HTML
"""

import argparse
import datetime
import html
import os
import re
import sys

from build_pdf import GREY, INK, MUTED, icon, inline, print_pdf, svg, t

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE = os.path.join(HERE, "..", "10-implementation-plan.md")
OUTPUT = os.path.join(HERE, "implementation-plan.pdf")

# --- Phases: what the Markdown doesn't hold -----------------------------------------------------------------------

PHASES = {
    "0": dict(short="Decide & prepare", icon="clipboard", color="#334155", tint="#f1f5f9"),
    "1": dict(short="Visibility", icon="gauge", color="#1d4ed8", tint="#eff6ff"),
    "2": dict(short="Container defaults", icon="sliders", color="#0f766e", tint="#f0fdfa"),
    "3": dict(short="Allocation as code", icon="layers", color="#6d28d9", tint="#f5f3ff"),
    "4": dict(short="Soft quotas", icon="shield", color="#b45309", tint="#fffbeb"),
    "5": dict(short="Budget quotas", icon="coins", color="#be185d", tint="#fdf2f8"),
    "6": dict(short="Steady state", icon="cycle", color="#15803d", tint="#f0fdf4"),
}
SUBTITLE = ("From today's state to budget-backed Rancher Project quotas on every cluster: where we are, then each "
            "step with its owner, how to do it and when it is done — container defaults (LimitRange), Project quotas, "
            "N+1 across every layer and the capacity planning views.")
SECTION_ICONS = {"1": "trend", "2": "priority", "3": "calendar", "4": "nodes", "5": "lock", "6": "gauge",
                 "7": "calendar", "8": "alert"}

# --- Markdown (the subset the doc uses) -------------------------------------------------------------------------


def parse(text):
    """Return a list of blocks: (kind, data). Kinds: h1, h2, h3, p, ul, ol, table, mermaid."""
    blocks, lines, i = [], text.splitlines(), 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        m = re.match(r"^(#{1,3}) (.+)$", line)
        if m:
            blocks.append((f"h{len(m.group(1))}", m.group(2)))
            i += 1
            continue
        if line.startswith("```"):
            lang, body = line[3:].strip(), []
            i += 1
            while not lines[i].startswith("```"):
                body.append(lines[i])
                i += 1
            blocks.append((lang or "code", "\n".join(body)))
            i += 1
            continue
        if line.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                cells = [c.strip() for c in re.split(r"(?<!\\)\|", lines[i].strip())[1:-1]]
                if not all(re.fullmatch(r":?-+:?", c) for c in cells):
                    rows.append(cells)
                i += 1
            blocks.append(("table", rows))
            continue
        m = re.match(r"^(- |\d+\. )", line)
        if m:
            kind, items = ("ul" if line.startswith("- ") else "ol"), []
            while i < len(lines) and lines[i].strip():
                m = re.match(r"^(- |\d+\. )(.*)$", lines[i])
                if m:
                    items.append(m.group(2).strip())
                else:
                    items[-1] += " " + lines[i].strip()
                i += 1
            blocks.append((kind, items))
            continue
        para = []
        while i < len(lines) and lines[i].strip() and not re.match(r"^(#|```|\||- |\d+\. )", lines[i]):
            para.append(lines[i].strip())
            i += 1
        blocks.append(("p", " ".join(para)))
    return blocks


# --- Diagrams (inline SVG, 640 wide) ----------------------------------------------------------------------------


def box(x, y, w, h, lines, color, tint, size=10.5):
    out = f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="7" fill="{tint}" stroke="{color}" stroke-width="1.4"/>'
    top = y + h / 2 - (len(lines) - 1) * (size + 2) / 2 + size / 3
    for n, s in enumerate(lines):
        out += t(x + w / 2, top + n * (size + 2), s, size if n == 0 else size - 1.5, color if n == 0 else MUTED,
                 "middle", 700 if n == 0 else 400)
    return out


def arrow(x1, y1, x2, y2, label=""):
    out = (f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{MUTED}" stroke-width="1.3" '
           f'marker-end="url(#arr)"/>')
    if label:
        out += t((x1 + x2) / 2, (y1 + y2) / 2 - 4, label, 8.5, MUTED, "middle")
    return out


def fig_phases():
    """Phase order: 0 → 1 → 2 → 4 → 5 → 6, with 3 in parallel from 0 into 4."""
    w, h = 112, 46
    pos = {"0": (0, 14), "1": (128, 14), "2": (256, 14), "3": (200, 96), "4": (400, 50), "5": (528, 50),
           "6": (656, 50)}
    sub = {"0": "decide, inventory", "1": "observe, audit", "2": "LimitRange", "3": "repo, Terraform, CI",
           "4": "footprint × 1.3", "5": "rates, enforce", "6": "reviews, cycle"}
    b = ""
    for k, (x, y) in pos.items():
        p = PHASES[k]
        b += box(x, y, w, h, [f"{k}. {p['short']}", sub[k]], p["color"], p["tint"], 10)
    b += arrow(112, 37, 126, 37) + arrow(240, 37, 254, 37)
    b += arrow(368, 37, 398, 64) + arrow(312, 119, 398, 84)
    b += f'<path d="M56 60 V119 H198" fill="none" stroke="{MUTED}" stroke-width="1.3" marker-end="url(#arr)"/>'
    b += arrow(512, 73, 526, 73) + arrow(640, 73, 654, 73)
    return svg(150, b, 772)


def fig_admission():
    """What happens to a new pod: LimitRange, then ResourceQuota, then the scheduler."""
    blue, red, green = ("#1d4ed8", "#eff6ff"), ("#dc2626", "#fef2f2"), ("#15803d", "#f0fdf4")
    b = box(0, 50, 80, 40, ["New pod"], INK, "#f8fafc")
    b += box(110, 40, 130, 60, ["LimitRange", "fills missing requests", "and limits; rejects > max"], *blue, 9.5)
    b += box(270, 40, 130, 60, ["ResourceQuota", "value set?", "fits the hard limit?"], *blue, 9.5)
    b += box(430, 40, 100, 60, ["Scheduler", "a node with free", "requests?"], *green, 9.5)
    b += box(560, 50, 80, 40, ["Running"], *green)
    b += box(270, 135, 130, 40, ["Rejected", "must specify / exceeded"], *red, 9.5)
    b += box(430, 135, 100, 40, ["Pending", "no node fits"], "#b45309", "#fffbeb", 9.5)
    b += arrow(80, 70, 108, 70) + arrow(240, 70, 268, 70) + arrow(400, 70, 428, 70, "fits") + arrow(530, 70, 558, 70)
    b += arrow(335, 100, 335, 133) + arrow(480, 100, 480, 133)
    b += t(175, 120, "admission: new pods only", 8.5, MUTED, "middle")
    b += t(175, 132, "running pods are never touched", 8.5, MUTED, "middle")
    return svg(185, b, 640)


def fig_gantt(src):
    """Draw the doc's gantt block: tasks with a start date or 'after <id>' and a duration in weeks."""
    tasks, ends, section = [], {}, ""
    for line in src.splitlines():
        line = line.strip()
        if line.startswith("section "):
            section = line[8:]
            continue
        m = re.match(r"^(.+?)\s*:(\w+),\s*(\S+(?: \w+)?),\s*(\d+)w$", line)
        if not m:
            continue
        name, tid, start, weeks = m.group(1), m.group(2), m.group(3), int(m.group(4))
        if start.startswith("after "):
            begin = ends[start[6:]]
        else:
            begin = datetime.date.fromisoformat(start)
        end = begin + datetime.timedelta(weeks=weeks)
        ends[tid] = end
        tasks.append((section, name, begin, end))
    if not tasks:
        sys.exit("Gantt block not understood")
    t0 = min(x[2] for x in tasks)
    total = max((x[3] - t0).days for x in tasks) / 7
    left, width, row = 190, 440, 24
    scale = width / total
    b = ""
    for wk in range(0, int(total) + 1, 2):
        x = left + wk * scale
        b += f'<line x1="{x}" y1="16" x2="{x}" y2="{22 + row * len(tasks)}" stroke="#e2e8f0"/>'
        b += t(x, 11, f"W{wk + 1}", 8.5, MUTED, "middle")
    for n, (sec, name, begin, end) in enumerate(tasks):
        y = 20 + n * row
        phase = PHASES[re.match(r"(\d)", name).group(1)]
        x = left + (begin - t0).days / 7 * scale
        w = (end - begin).days / 7 * scale
        b += t(0, y + 14, name, 9.5, INK)
        b += (f'<rect x="{x}" y="{y + 3}" width="{w}" height="{row - 8}" rx="4" fill="{phase["color"]}" '
              f'opacity=".88"/>')
    return svg(28 + row * len(tasks), b, 640)


def figure(lang, body):
    if "Pod[New pod]" in body:
        return fig_admission()
    if body.lstrip().startswith("gantt"):
        return fig_gantt(body)
    if "P0[0." in body and "P6[6." in body:
        return fig_phases()
    sys.exit(f"Unknown {lang} diagram in the doc; add a drawing for it:\n{body[:200]}")


# --- Page ---------------------------------------------------------------------------------------------------------

CSS = """
@page { size: A4; margin: 14mm 15mm 16mm 15mm;
  @bottom-left { content: "Implementation Plan · Cluster resource allocation"; font: 8pt system-ui, sans-serif; color: #64748b; }
  @bottom-right { content: counter(page) " / " counter(pages); font: 8pt system-ui, sans-serif; color: #64748b; } }
@page cover { @bottom-left { content: none; } @bottom-right { content: none; } }
* { box-sizing: border-box; -webkit-print-color-adjust: exact; print-color-adjust: exact; }
html { font: 9pt/1.38 -apple-system, "Segoe UI", "Helvetica Neue", Arial, sans-serif; color: #0f172a; }
body { margin: 0; background: #fff; }
code { font: 8.2pt ui-monospace, "SF Mono", Menlo, Consolas, monospace; background: #f1f5f9; padding: 0 3px; border-radius: 3px; }
b { font-weight: 650; }
svg text { font-family: -apple-system, "Segoe UI", "Helvetica Neue", Arial, sans-serif; }
.ic { vertical-align: middle; flex: none; }
p { margin: 0 0 2mm; }
ul, ol { margin: 0 0 2mm; padding-left: 5mm; }
li { margin: 0 0 0.6mm; break-inside: avoid; }
.cover { page: cover; height: 265mm; display: flex; flex-direction: column; }
.cover .kicker { font-size: 9pt; letter-spacing: .14em; text-transform: uppercase; color: #64748b; margin-top: 18mm; }
.cover h1 { font-size: 34pt; line-height: 1.05; margin: 4mm 0 3mm; letter-spacing: -.02em; }
.cover .sub { font-size: 12pt; color: #334155; max-width: 155mm; }
.cover .band { display: flex; gap: 2mm; margin: 9mm 0 6mm; }
.cover .band span { flex: 1; height: 3mm; border-radius: 2mm; }
.cover .fig { margin: 0 0 6mm; }
.verdict { border-left: 1.4mm solid #0f172a; background: #f8fafc; border-radius: 0 2mm 2mm 0; padding: 3mm 4mm; font-size: 11pt; margin-bottom: 5mm; }
.cards { display: grid; grid-template-columns: repeat(4, 1fr); gap: 2.5mm; }
.card { border: 1px solid; border-radius: 3mm; padding: 2.5mm 3mm; font-size: 8pt; line-height: 1.3; }
.card h3 { margin: 1mm 0 1mm; font-size: 9.5pt; }
.card .n { font-size: 7pt; color: #475569; }
.cover .meta { margin-top: auto; font-size: 8.5pt; color: #64748b; display: flex; justify-content: space-between; border-top: 1px solid #e2e8f0; padding-top: 3mm; }
h2.sec { display: flex; align-items: center; gap: 3mm; font-size: 16pt; margin: 0 0 3mm; padding-bottom: 2mm; border-bottom: 2px solid #0f172a; letter-spacing: -.01em; break-after: avoid; }
section.sec { break-before: page; }
section.sec.flow { break-before: auto; margin-top: 7mm; }
h3.sub { font-size: 11.5pt; margin: 4mm 0 2mm; break-after: avoid; }
table { width: 100%; border-collapse: collapse; margin: 0 0 3mm; font-size: 8.2pt; line-height: 1.3; }
th { text-align: left; background: #f1f5f9; padding: 1.3mm 1.8mm; border-bottom: 1.5px solid #cbd5e1; font-weight: 650; }
td { padding: 1.2mm 1.8mm; border-bottom: 1px solid #e2e8f0; vertical-align: top; }
td:first-child { font-weight: 600; }
tr { break-inside: avoid; }
thead { display: table-header-group; }
.figure { border: 1px solid #e2e8f0; border-radius: 2.5mm; padding: 2mm 3mm 1mm; margin: 0 0 3mm; break-inside: avoid; text-align: center; }
.figure svg { width: 96%; }
.phase { display: flex; gap: 4mm; align-items: center; border-radius: 3mm; padding: 2.5mm 4.5mm; margin: 5mm 0 3mm; color: #fff; break-after: avoid; }
.phase .big { width: 12mm; height: 12mm; border-radius: 50%; background: rgba(255,255,255,.18); display: flex; align-items: center; justify-content: center; }
.phase h3 { margin: 0; font-size: 14pt; }
.phase .tag { font-size: 9pt; opacity: .92; }
.step { border-left: 1.2mm solid; padding: 0.5mm 0 0.5mm 3.5mm; margin: 0 0 3.5mm; }
.step-head { display: flex; align-items: baseline; gap: 2mm; flex-wrap: wrap; margin-bottom: 1.2mm; break-after: avoid; }
.step-head .id { color: #fff; font-weight: 800; border-radius: 1.5mm; padding: 0.3mm 1.8mm; font-size: 8.6pt; }
.step-head .title { font-weight: 700; font-size: 10.5pt; }
.owner { font-size: 7.6pt; border: 1px solid #cbd5e1; border-radius: 5mm; padding: 0.2mm 2.2mm; color: #475569; }
.done { display: flex; gap: 2mm; align-items: flex-start; background: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 2mm; padding: 1.3mm 2.5mm; margin: 1mm 0 0; break-inside: avoid; }
.done .ic { color: #15803d; margin-top: 0.3mm; }
.done b { color: #15803d; }
"""


def done_box(text):
    return (f'<div class="done">{icon("check", 15, stroke=2.4)}<span><b>Done when:</b> {inline(text)}</span></div>')


def paragraph(text):
    """A paragraph; a trailing 'Done when: …' sentence becomes a done box."""
    if "Done when:" in text:
        before, after = text.split("Done when:", 1)
        return (f"<p>{inline(before.strip())}</p>" if before.strip() else "") + done_box(after.strip())
    return f"<p>{inline(text)}</p>"


def listing(kind, items):
    done = [x for x in items if x.startswith("Done when:")]
    rest = [x for x in items if not x.startswith("Done when:")]
    out = f"<{kind}>" + "".join(f"<li>{inline(x)}</li>" for x in rest) + f"</{kind}>" if rest else ""
    return out + "".join(done_box(x[len("Done when:"):].strip()) for x in done)


def table(rows):
    head = "".join(f"<th>{inline(c)}</th>" for c in rows[0])
    body = "".join("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>" for r in rows[1:])
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def step_open(text, phase):
    """'**Step 0.1 Title.** Owner: X. Rest' → step card header and the rest of the paragraph."""
    m = re.match(r"^\*\*Step (\d+\.\d+) (.+?)\*\*\s*Owner: (.+?)\.(?:\s+(.*))?$", text)
    if not m:
        sys.exit(f"Step paragraph not understood: {text[:80]}")
    sid, title, owner, rest = m.groups()
    p = PHASES[phase]
    out = (f'<div class="step" style="border-color:{p["color"]}"><div class="step-head">'
           f'<span class="id" style="background:{p["color"]}">{sid}</span>'
           f'<span class="title">{inline(title.rstrip("."))}</span><span class="owner">Owner: {inline(owner)}</span></div>')
    return out + (paragraph(rest) if rest else "")


def cover(blocks):
    """Title, phase band and diagram, the status verdict and one card per phase."""
    today = datetime.date.today().strftime("%d %B %Y").lstrip("0")
    verdict = next((d for k, d in blocks if k == "p" and d.startswith("In short:")), "")
    band = "".join(f'<span style="background:{p["color"]}"></span>' for p in PHASES.values())
    steps = {}
    for k, d in blocks:
        m = re.match(r"^\*\*Step (\d)\.\d+", d) if k == "p" else None
        if m:
            steps[m.group(1)] = steps.get(m.group(1), 0) + 1
    titles = {m.group(1): m.group(2) for k, d in blocks if k == "h3" for m in [re.match(r"^Phase (\d)\. (.+)$", d)] if m}
    cards = ""
    for n, p in PHASES.items():
        when = re.search(r"\(([^)]*)\)", titles.get(n, ""))
        cards += (f'<div class="card" style="border-color:{p["color"]};background:{p["tint"]}">'
                  f'<span style="color:{p["color"]}">{icon(p["icon"], 20)}</span>'
                  f'<h3 style="color:{p["color"]}">{n}. {html.escape(p["short"])}</h3>'
                  f'<div class="n">{steps.get(n, 0)} step{"s" if steps.get(n, 0) != 1 else ""}'
                  f'{" · " + html.escape(when.group(1)) if when else ""}</div></div>')
    return (f'<div class="cover"><div class="kicker">Cluster resource allocation · Platform team</div>'
            f'<h1>Implementation<br>Plan</h1><div class="sub">{html.escape(SUBTITLE)}</div>'
            f'<div class="band">{band}</div><div class="fig">{fig_phases()}</div>'
            f'<div class="verdict">{inline(verdict)}</div><div class="cards">{cards}</div>'
            f'<div class="meta"><span>Source: docs/10-implementation-plan.md</span><span>Printed {today}</span>'
            f'</div></div>')


def render(blocks):
    out = [f"<!doctype html><html lang='en'><head><meta charset='utf-8'><title>Implementation Plan</title>"
           f"<style>{CSS}</style></head><body>", cover(blocks)]
    phase, in_step, in_section = None, False, False

    def close_step():
        nonlocal in_step
        if in_step:
            out.append("</div>")
            in_step = False

    for kind, data in blocks:
        if kind == "h1":
            continue
        if kind == "h2":
            close_step()
            if in_section:
                out.append("</section>")
            num = data.split(".")[0]
            # Short sections follow the previous one on the same page.
            flow = " flow" if num in ("6", "7", "8") else ""
            out.append(f'<section class="sec{flow}"><h2 class="sec">{icon(SECTION_ICONS.get(num, "check"), 22)}'
                       f'{inline(data)}</h2>')
            in_section, phase = True, None
            continue
        if kind == "h3":
            close_step()
            m = re.match(r"^Phase (\d)\. (.+?)(?: \((.+)\))?$", data)
            if m:
                phase = m.group(1)
                p = PHASES[phase]
                out.append(f'<div class="phase" style="background:{p["color"]}"><div class="big">'
                           f'{icon(p["icon"], 26, "#fff")}</div><div><h3>Phase {phase}. {inline(m.group(2))}</h3>'
                           f'<div class="tag">{inline(m.group(3) or "")}</div></div></div>')
            else:
                out.append(f'<h3 class="sub">{inline(data)}</h3>')
            continue
        if kind == "p" and data.startswith("**Step "):
            close_step()
            out.append(step_open(data, phase))
            in_step = True
            continue
        if kind == "p":
            if data.startswith("Step-by-step guide") and not in_section:
                continue  # the intro is on the cover
            out.append(paragraph(data))
        elif kind in ("ul", "ol"):
            out.append(listing(kind, data))
        elif kind == "table":
            out.append(table(data))
        elif kind == "mermaid":
            if "P0[0." in data:
                continue  # already on the cover
            out.append(f'<div class="figure">{figure(kind, data)}</div>')
        else:
            out.append(f"<pre><code>{html.escape(data)}</code></pre>")
    close_step()
    out.append("</section></body></html>")
    return "".join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=OUTPUT, help="PDF to write (default: %(default)s)")
    ap.add_argument("--html", help="also write the intermediate HTML here")
    args = ap.parse_args()

    with open(SOURCE, encoding="utf-8") as f:
        blocks = parse(f.read())
    phases = {m.group(1) for k, d in blocks if k == "h3" for m in [re.match(r"^Phase (\d)\.", d)] if m}
    if phases != set(PHASES):
        sys.exit(f"Doc structure changed: phases {sorted(phases)}, expected {sorted(PHASES)}")
    print_pdf(render(blocks), args.out, args.html, "implementation-plan.html")
    print(args.out)


if __name__ == "__main__":
    main()
