#!/usr/bin/env python3
"""Build a Word file of a doc (default docs/09-operating-principles.md) for import into Confluence.

One .docx holds the text, tables and diagrams: every Mermaid block is rendered to a PNG and embedded, so Confluence's
Word import (Data Center: page ⋯ → Import Word document) creates the page and its image attachment in one step.
Links to other files become plain text, links within the doc stay as anchors. Needs `mmdc` (npm
@mermaid-js/mermaid-cli) and `pandoc` on the PATH; otherwise standard library only.

    python3 docs/print/build_docx.py                       # writes docs/print/operating-principles.docx
    python3 docs/print/build_docx.py docs/05-process.md -o process.docx
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
SOURCE = os.path.join(REPO, "docs", "09-operating-principles.md")
OUTPUT = os.path.join(HERE, "operating-principles.docx")

MERMAID = re.compile(r"^```mermaid\n(.*?)^```\n", re.S | re.M)
LINK = re.compile(r"(?<!!)\[([^\]]+)\]\((?!#)[^)\s]+\)")


def need(tool, hint):
    if not shutil.which(tool):
        sys.exit(f"{tool} not found: {hint}")


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


def drop_links(text):
    """Keep only the text of links that leave the doc; the targets aren't in Confluence. In-doc anchors stay."""
    return LINK.sub(r"\1", text)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("source", nargs="?", default=SOURCE, help="Markdown file (default: %(default)s)")
    ap.add_argument("-o", "--out", default=None, help=f"Word file to write (default: {OUTPUT} for the default "
                                                     "source, else <source>.docx)")
    args = ap.parse_args()
    out = args.out or (OUTPUT if args.source == SOURCE else os.path.splitext(args.source)[0] + ".docx")

    need("mmdc", "npm install -g @mermaid-js/mermaid-cli")
    need("pandoc", "brew install pandoc")
    with open(args.source, encoding="utf-8") as f:
        text = f.read()

    with tempfile.TemporaryDirectory() as tmp:
        text = render_mermaid(drop_links(text), tmp)
        md = os.path.join(tmp, "doc.md")
        with open(md, "w", encoding="utf-8") as f:
            f.write(text)
        # gfm: same heading anchors as GitHub, so the in-doc links keep working.
        subprocess.run(["pandoc", "-f", "gfm", "-t", "docx", md, "-o", os.path.abspath(out)], check=True, cwd=tmp)
    print(out)


if __name__ == "__main__":
    main()
