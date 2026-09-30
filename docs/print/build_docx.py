#!/usr/bin/env python3
"""Build a Word file of a doc (default docs/09-operating-principles.md) for import into Confluence.

One .docx holds text, tables and images, so Confluence's Word import (Data Center: page ⋯ → Import Word document)
creates the page and its image attachments in one step. Links to other files become plain text (they aren't in
Confluence); links within the doc stay as anchors.

The operating principles get the PDF's visuals (build_pdf.py): the cover with the category cards, the at-a-glance
table, a banner per category, and each topic's rule and diagram, as images; headings and bullets stay text. Any other
doc is converted as it is, with each Mermaid block rendered to an image.

Needs `mmdc` (npm @mermaid-js/mermaid-cli; its bundled headless Chrome also takes the screenshots) and `pandoc`.

    python3 docs/print/build_docx.py                       # writes docs/print/operating-principles.docx
    python3 docs/print/build_docx.py docs/05-process.md -o process.docx
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import build_pdf as pdf  # noqa: E402

SOURCE = os.path.abspath(pdf.SOURCE)
OUTPUT = os.path.join(HERE, "operating-principles.docx")

MERMAID = re.compile(r"^```mermaid\n(.*?)^```\n", re.S | re.M)
LINK = re.compile(r"(?<!!)\[([^\]]+)\]\((?!#)[^)\s]+\)")

# Screenshots at A4 text width (180 mm = 680 px), twice the resolution for sharp images.
PAGE_PX = 680
SHOT_CSS = pdf.CSS + f"""
body {{ width: {PAGE_PX}px; padding: 4px; }}
.shot {{ display: flow-root; padding: 1px; }}
.shot.cover {{ height: auto; }}
.shot .band {{ margin-top: 0; }}
.shot > :last-child, .shot .intro {{ margin-bottom: 0; }}
"""

SCREENSHOT_JS = """
const {createRequire} = require("module");
const puppeteer = createRequire(process.argv[2])("puppeteer");
(async () => {
  const browser = await puppeteer.launch({headless: "new"});
  const page = await browser.newPage();
  await page.setViewport({width: 720, height: 1000, deviceScaleFactor: 2});
  await page.goto("file://" + process.argv[3], {waitUntil: "load"});
  const sizes = {};
  for (const el of await page.$$(".shot")) {
    const id = await el.evaluate(e => e.id);
    sizes[id] = (await el.boundingBox()).width;
    await el.screenshot({path: process.argv[4] + "/" + id + ".png"});
  }
  console.log(JSON.stringify(sizes));
  await browser.close();
})().catch(e => { console.error(e); process.exit(1); });
"""


def need(tool, hint):
    if not shutil.which(tool):
        sys.exit(f"{tool} not found: {hint}")


def drop_links(text):
    """Keep only the text of links that leave the doc; the targets aren't in Confluence. In-doc anchors stay."""
    return LINK.sub(r"\1", text)


def render_mermaid(text, tmp):
    """Replace each Mermaid block with an image of it, rendered by mmdc."""
    count = 0

    def one(m):
        nonlocal count
        count += 1
        src, png = os.path.join(tmp, f"diagram-{count}.mmd"), os.path.join(tmp, f"diagram-{count}.png")
        with open(src, "w", encoding="utf-8") as f:
            f.write(m.group(1))
        subprocess.run(["mmdc", "-q", "-i", src, "-o", png, "-s", "2", "-b", "white"], check=True)
        return f"![](diagram-{count}.png)\n"

    return MERMAID.sub(one, text)


def screenshot(shots, tmp):
    """Render {id: html} with the PDF's styles and save each as <id>.png; return {id: width in px}."""
    body = "".join(f'<div class="shot{" cover" if id_ == "cover" else ""}" id="{id_}">{h}</div>'
                   for id_, h in shots.items())
    page = os.path.join(tmp, "shots.html")
    with open(page, "w", encoding="utf-8") as f:
        f.write(f"<!doctype html><html><head><meta charset='utf-8'><style>{SHOT_CSS}</style></head>"
                f"<body>{body}</body></html>")
    js = os.path.join(tmp, "shots.js")
    with open(js, "w", encoding="utf-8") as f:
        f.write(SCREENSHOT_JS)
    mmdc = os.path.realpath(shutil.which("mmdc"))
    out = subprocess.run(["node", js, mmdc, page, tmp], check=True, capture_output=True, text=True).stdout
    return json.loads(out.strip().splitlines()[-1])


def principles_markdown(text, tmp):
    """The operating principles with the PDF's visuals as images."""
    cats = pdf.parse(text)
    shots, alts, md = {"cover": pdf.cover_visual(cats), "glance": pdf.glance_table(cats)}, {}, []
    alts["cover"] = "Categories: " + "; ".join(f"{k}. {pdf.CATEGORIES[k]['name']}" for k in pdf.ORDER)
    alts["glance"] = "At a glance: " + "; ".join(f"{tp['id']} {pdf.TOPICS[tp['id']][1]}"
                                                 for k in pdf.ORDER for tp in cats[k]["topics"])
    for k in pdf.ORDER:
        c, cat = pdf.CATEGORIES[k], cats[k]
        shots[k] = pdf.banner(k, cat)
        alts[k] = f"{k}. {c['name']} — {c['tag']}. Platform team: {c['platform']}. Teams: {c['teams']}."
        for tp in cat["topics"]:
            shots[tp["id"]] = pdf.rule_and_figure(k, tp)
            alts[tp["id"]] = pdf.TOPICS[tp["id"]][1]
            if tp["id"] == "E2":
                shots["E2-alerts"] = pdf.alert_cards(k, tp)
                alts["E2-alerts"] = "; ".join(re.sub(r"[*`]", "", b) for b in tp["bullets"])
    widths = screenshot(shots, tmp)

    def img(id_):
        pct = min(100, round(100 * widths[id_] / PAGE_PX))
        alt = re.sub(r"([\\\[\]*_`<>])", r"\\\1", alts[id_])
        return f"![{alt}]({id_}.png){{width={pct}%}}\n"

    md += ["# Operating Principles\n", pdf.SUBTITLE + "\n", img("cover"), "## At a glance\n",
           pdf.GLANCE_LEDE + "\n", img("glance")]
    for k in pdf.ORDER:
        c = pdf.CATEGORIES[k]
        md += [f"## {k}. {c['name']} — {c['tag']}\n", img(k)]
        for tp in cats[k]["topics"]:
            md += [f"### {tp['id']}. {tp['title']}\n", img(tp["id"])]
            if tp["lead"].strip():
                md.append(tp["lead"].strip() + "\n")
            if tp["id"] == "E2":
                md.append(img("E2-alerts"))
            else:
                md.append("".join(f"- {b}\n" for b in tp["bullets"]))
    return drop_links("\n".join(md))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("source", nargs="?", default=SOURCE, help="Markdown file (default: %(default)s)")
    ap.add_argument("-o", "--out", default=None, help=f"Word file to write (default: {OUTPUT} for the default "
                                                     "source, else <source>.docx)")
    args = ap.parse_args()
    source = os.path.abspath(args.source)
    out = os.path.abspath(args.out or (OUTPUT if source == SOURCE else os.path.splitext(source)[0] + ".docx"))

    need("mmdc", "npm install -g @mermaid-js/mermaid-cli")
    need("pandoc", "brew install pandoc")
    with open(source, encoding="utf-8") as f:
        text = f.read()

    with tempfile.TemporaryDirectory() as tmp:
        if source == SOURCE:
            text = principles_markdown(text, tmp)
        else:
            text = render_mermaid(drop_links(text), tmp)
        md = os.path.join(tmp, "doc.md")
        with open(md, "w", encoding="utf-8") as f:
            f.write(text)
        # GitHub's heading anchors, so the in-doc links keep working; image widths as attributes.
        fmt = ("markdown+gfm_auto_identifiers-implicit_figures-subscript-superscript-tex_math_dollars"
               "-smart") if source == SOURCE else "gfm"
        subprocess.run(["pandoc", "-f", fmt, "-t", "docx", md, "-o", out], check=True, cwd=tmp)
    print(os.path.relpath(out))


if __name__ == "__main__":
    main()
