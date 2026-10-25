"""Build the Steering Ledger report as HTML and PDF.

Usage:  python make_pdf.py

Reads  steering_ledger_template.html  (this directory), embeds the figure
PNGs from  lab/analysis/figures/  as data URIs, writes the assembled
steering_ledger.html, then prints it to steering_ledger.pdf with headless
Chrome. Rerun after editing the template or regenerating any figure.
"""

from __future__ import annotations

import base64
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
LAB = HERE.parents[1]

FIGS = {
    "{{QFREE}}": "analysis/figures/ledger/qwen_freeform.png",
    "{{FORECAST}}": "analysis/figures/emoji_methods/forecast.png",
    "{{QSINGLES}}": "analysis/figures/emoji_methods_v2/corrected_singles.png",
    "{{GSINGLES}}": "analysis/figures/gemma_core/gemma_singles.png",
    "{{QPHRASES}}": "analysis/figures/ledger/qwen_phrases.png",
    "{{GMETH2}}": "analysis/figures/ledger/gemma_methods.png",
    "{{QSPLIT}}": "analysis/figures/emoji_methods_v2/split_verdict_v2.png",
    "{{GSPLIT}}": "analysis/figures/gemma_core/gemma_split.png",
    "{{DWELL}}": "analysis/figures/gemma_core/dwell_crossmodel.png",
}

# Print-only additions: keep every background color, stop page breaks from
# cutting charts, boxes, and table rows in half, and let wide tables lay out
# on the page instead of scrolling.
PRINT_CSS = """
@page { size: A4; margin: 13mm 11mm; }
@media print {
  html { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
  body { padding: 0; }
  .wrap { max-width: 100%; gap: 2rem; }
  .card, .box, .diffwell, figure, .prompts { break-inside: avoid; }
  tr { break-inside: avoid; }
  h1, h2, h3, .eyebrow { break-after: avoid; break-inside: avoid; }
  .cap { break-before: avoid; break-inside: avoid; }
  .print-page-break { break-before: page; page-break-before: always; }
  .scroll { overflow-x: visible; }
  a { color: inherit; text-decoration: none; }
}
"""

CHROME_CANDIDATES = [
    Path(r"C:/Program Files/Google/Chrome/Application/chrome.exe"),
    Path(r"C:/Program Files (x86)/Google/Chrome/Application/chrome.exe"),
]


def assemble() -> Path:
    html = (HERE / "steering_ledger_template.html").read_text(encoding="utf-8")
    for token, rel in FIGS.items():
        png = (LAB / rel).read_bytes()
        if token not in html:
            sys.exit(f"template is missing {token}")
        uri = "data:image/png;base64," + base64.b64encode(png).decode()
        html = html.replace(token, uri)
    leftover = re.findall(r"\{\{[A-Z0-9_]+\}\}", html)
    if leftover:
        sys.exit(f"unmapped figure tokens: {leftover}")
    # Wrap each table's header row in <thead> so Chrome repeats it when a
    # table breaks across pages.
    html = re.sub(r"(<table[^>]*>)\s*(<tr>(?:\s*<th[\s\S]*?)</tr>)",
                  r"\1<thead>\2</thead>", html)
    html = html.replace("</style>", PRINT_CSS + "</style>", 1)
    out = HERE / "steering_ledger.html"
    out.write_text(html, encoding="utf-8")
    return out


def print_pdf(page: Path) -> Path:
    chrome = next((c for c in CHROME_CANDIDATES if c.exists()), None)
    if chrome is None:
        sys.exit("Chrome not found; edit CHROME_CANDIDATES in this script")
    pdf = HERE / "steering_ledger.pdf"
    cmd = [
        str(chrome),
        "--headless",
        "--disable-gpu",
        "--no-pdf-header-footer",
        f"--print-to-pdf={pdf}",
        page.resolve().as_uri(),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    return pdf


if __name__ == "__main__":
    page = assemble()
    pdf = print_pdf(page)
    print(f"wrote {page.name} ({page.stat().st_size // 1024} KB) "
          f"and {pdf.name} ({pdf.stat().st_size // 1024} KB)")
