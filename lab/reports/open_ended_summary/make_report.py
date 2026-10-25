"""Build the 69-file open-ended steering summary as standalone HTML and PDF.

Usage (from ``lab`` or the repository root):

    python reports/open_ended_summary/make_report.py
    python lab/reports/open_ended_summary/make_report.py

The builder audits the campaign count against the experiment manifest, embeds
the reviewed result figures as data URIs, computes the reported deduplicated
correlations from the checked-in summary, and prints the light-mode HTML to an
A4 PDF with headless Chrome.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import re
import subprocess
import sys
from pathlib import Path


HERE = Path(__file__).resolve().parent
LAB = HERE.parents[1]
ROOT = LAB.parent
TEMPLATE = HERE / "open_ended_summary_template.html"
HTML_OUT = ROOT / "output" / "html" / "nla_open_ended_69_run_summary.html"
PDF_OUT = ROOT / "output" / "pdf" / "nla_open_ended_69_run_summary.pdf"

CAMPAIGNS = {
    "emoji_methods_wave1": (14, 5_250),
    "emoji_methods_wave2": (44, 16_500),
    "wordle2": (11, 3_060),
}

FIGURES = {
    "{{CORRECTED_SINGLES}}": LAB
    / "analysis/figures/emoji_methods_v2/corrected_singles.png",
    "{{READOUT_ALIGNMENT}}": LAB
    / "analysis/figures/emoji_methods/forecast.png",
    "{{JSPACE_SPLIT}}": LAB
    / "analysis/figures/emoji_methods_v2/split_verdict_v2.png",
    "{{DWELLING}}": LAB / "analysis/figures/wordle2/dwelling_outcomes.png",
}

CHROME_CANDIDATES = [
    Path(r"C:/Program Files/Google/Chrome/Application/chrome.exe"),
    Path(r"C:/Program Files (x86)/Google/Chrome/Application/chrome.exe"),
]


def _rank(values: list[float]) -> list[float]:
    """Return average ranks, sufficient for the small tie-aware audit."""

    order = sorted(range(len(values)), key=values.__getitem__)
    ranks = [0.0] * len(values)
    start = 0
    while start < len(order):
        end = start + 1
        while end < len(order) and values[order[end]] == values[order[start]]:
            end += 1
        average = (start + 1 + end) / 2
        for index in order[start:end]:
            ranks[index] = average
        start = end
    return ranks


def _correlation(xs: list[float], ys: list[float]) -> float:
    x_mean = sum(xs) / len(xs)
    y_mean = sum(ys) / len(ys)
    numerator = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys))
    x_ss = sum((x - x_mean) ** 2 for x in xs)
    y_ss = sum((y - y_mean) ** 2 for y in ys)
    denominator = math.sqrt(x_ss * y_ss)
    if denominator == 0:
        raise ValueError("correlation is undefined for a constant series")
    return numerator / denominator


def _unique_correlation_rows() -> list[tuple[str, float, float]]:
    """Return one row per unique parent vector for the k25-share audit.

    ``s2_emoji`` is excluded because it is an exact rebuild of ``single_5``.
    The retained ``single_5`` effect pools both seed waves; the remaining four
    corrected target arms use wave-2-only samples, matching ``summary_v2``.
    """

    summary = json.loads(
        (LAB / "analysis/figures/emoji_methods_v2/summary_v2.json").read_text(
            encoding="utf-8"
        )
    )
    arms = (
        ["freeform"]
        + [f"single_{i}" for i in range(1, 6)]
        + [f"phrase_{i}" for i in range(1, 6)]
        + ["s2_emojis", "s2_emoticon", "s2_smiley", "s2_glyph"]
    )
    rows = [
        (
            arm,
            float(summary[arm]["share_k25"]),
            float(summary[arm]["best"]["delta"]),
        )
        for arm in arms
    ]
    if len(rows) != 15:
        raise SystemExit(f"expected 15 unique correlation rows, found {len(rows)}")
    return rows


def _scatter_uri() -> str:
    """Build a report-specific, deduplicated k25-share scatter."""

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    rows = _unique_correlation_rows()
    labels = {
        "freeform": "AR parent",
        "single_1": "excited",
        "single_2": "enthusiastic",
        "single_3": "cheerful",
        "single_4": "expressive",
        "single_5": "emoji (pooled)",
        "phrase_1": "phrase 1",
        "phrase_2": "phrase 2",
        "phrase_3": "phrase 3",
        "phrase_4": "glyph phrase",
        "phrase_5": "phrase 5",
        "s2_emojis": "emojis",
        "s2_emoticon": "emoticon",
        "s2_smiley": "smiley",
        "s2_glyph": "glyph",
    }
    offsets = {
        "single_1": (6, -10),
        "single_2": (6, -2),
        "single_3": (6, 7),
        "single_5": (6, 7),
        "s2_emoticon": (6, -12),
        "s2_emojis": (-30, -13),
        "phrase_2": (-48, 8),
        "phrase_3": (7, -3),
        "phrase_5": (-38, 6),
        "s2_smiley": (6, 5),
        "s2_glyph": (-40, 6),
    }
    colors = {
        "freeform": "#0e7570",
        "single": "#7b6a58",
        "phrase": "#2d77d2",
        "target": "#6941c6",
    }
    markers = {"freeform": "o", "single": "^", "phrase": "s", "target": "D"}

    figure, axis = plt.subplots(figsize=(7.2, 4.7), facecolor="#fcfcfb")
    axis.set_facecolor("#fcfcfb")
    for arm, share, effect in rows:
        family = (
            "freeform"
            if arm == "freeform"
            else "single"
            if arm.startswith("single_")
            else "phrase"
            if arm.startswith("phrase_")
            else "target"
        )
        axis.scatter(
            share,
            effect,
            s=58,
            color=colors[family],
            marker=markers[family],
            edgecolors="#fcfcfb",
            linewidths=1.2,
            zorder=3,
        )
        dx, dy = offsets.get(arm, (6, 4))
        axis.annotate(
            labels[arm],
            (share, effect),
            xytext=(dx, dy),
            textcoords="offset points",
            fontsize=7.5,
            color="#4f5359",
        )

    axis.grid(True, color="#dedbd2", linewidth=0.8, zorder=0)
    axis.spines[["top", "right"]].set_visible(False)
    axis.spines[["left", "bottom"]].set_color("#bcb8ae")
    axis.tick_params(colors="#5f625f", labelsize=8.5)
    axis.set_xlabel("k25 component-norm ratio  ||c25|| / ||d||", color="#4f5359")
    axis.set_ylabel("best coherence-safe emoji effect (Cliff delta)", color="#4f5359")
    axis.set_title(
        "Recovered k25 share did not rank emoji steering",
        color="#23272e",
        fontsize=12,
    )
    figure.tight_layout()
    buffer = io.BytesIO()
    figure.savefig(buffer, format="png", dpi=170, facecolor=figure.get_facecolor())
    plt.close(figure)
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def _length_null_uri() -> str:
    """Build the Wordle length comparison directly from archived records."""

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    manifest = json.loads(
        (LAB / "registry/experiment_manifest.json").read_text(encoding="utf-8")
    )
    entries = {
        entry["experiment_name"]: entry
        for entry in manifest["entries"]
        if entry["campaign"] == "wordle2"
    }
    arms = [
        ("wordle2-len-four-b", "aim four", "#26a269", "o", "-"),
        ("wordle2-len-six-b", "aim six", "#3579d4", "o", "-"),
        ("wordle2-len-seven-b", "aim seven", "#5844b5", "o", "-"),
        ("wordle2-len-digit-six-b", "aim 6 (digit)", "#e99b00", "o", "-"),
        ("wordle2-ff-a-six", "junk-readout control", "#e879a5", "o", "--"),
        ("wordle2-random", "random control", "#ed6a35", "o", "-"),
    ]

    figure, axis = plt.subplots(figsize=(7.2, 4.7), facecolor="#fcfcfb")
    axis.set_facecolor("#fcfcfb")
    for experiment_name, label, color, marker, linestyle in arms:
        entry = entries.get(experiment_name)
        if entry is None:
            raise SystemExit(f"missing Wordle result in manifest: {experiment_name}")
        result = json.loads((ROOT / entry["result_file"]).read_text(encoding="utf-8"))
        by_alpha: dict[float, list[float]] = {}
        for record in result["records"]:
            by_alpha.setdefault(float(record["alpha"]), []).append(
                float(record["probes"]["answer_letters"])
            )
        if sorted(len(values) for values in by_alpha.values()) != [60] * 5:
            raise SystemExit(f"unexpected Wordle cell counts for {experiment_name}")
        alphas = sorted(by_alpha)
        extracted = {
            alpha: [letters for letters in by_alpha[alpha] if letters > 0]
            for alpha in alphas
        }
        if any(not values for values in extracted.values()):
            raise SystemExit(f"no extracted Wordle answers for {experiment_name}")
        means = [
            sum(extracted[alpha]) / len(extracted[alpha]) for alpha in alphas
        ]
        axis.plot(
            alphas,
            means,
            color=color,
            marker=marker,
            linestyle=linestyle,
            linewidth=2.0,
            markersize=5,
            label=label,
        )

    axis.grid(True, color="#dedbd2", linewidth=0.8, zorder=0)
    axis.spines[["top", "right"]].set_visible(False)
    axis.spines[["left", "bottom"]].set_color("#bcb8ae")
    axis.tick_params(colors="#5f625f", labelsize=8.5)
    axis.set_xlim(-0.02, 0.82)
    axis.set_ylim(4.1, 5.65)
    axis.set_xlabel("steering strength (alpha)", color="#4f5359")
    axis.set_ylabel("mean letters in extracted answer", color="#4f5359")
    axis.set_title(
        "Tested length arms did not separate from random in this sweep",
        color="#23272e",
        fontsize=12,
    )
    axis.legend(frameon=False, fontsize=7.5, loc="lower left")
    figure.tight_layout()
    buffer = io.BytesIO()
    figure.savefig(buffer, format="png", dpi=170, facecolor=figure.get_facecolor())
    plt.close(figure)
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def audit_corpus() -> dict[str, str]:
    manifest_path = LAB / "registry/experiment_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries = [entry for entry in manifest["entries"] if entry["campaign"] in CAMPAIGNS]

    by_campaign: dict[str, tuple[int, int]] = {}
    for campaign in CAMPAIGNS:
        selected = [entry for entry in entries if entry["campaign"] == campaign]
        by_campaign[campaign] = (len(selected), sum(entry["n_records"] for entry in selected))
    if by_campaign != CAMPAIGNS:
        raise SystemExit(
            "69-run corpus audit failed; expected "
            f"{CAMPAIGNS!r}, found {by_campaign!r}"
        )

    if len(entries) != 69 or sum(entry["n_records"] for entry in entries) != 24_810:
        raise SystemExit("69-run corpus totals changed unexpectedly")

    root_counts: dict[str, int] = {}
    for entry in entries:
        root = entry["root_method"]
        root_counts[root] = root_counts.get(root, 0) + 1
    expected_roots = {
        "nla_ar_contrast": 15,
        "nla_av_edit_ar": 50,
        "random_isotropic": 3,
        "none": 1,
    }
    if root_counts != expected_roots:
        raise SystemExit(
            f"69-run root-method census changed: {root_counts!r} != {expected_roots!r}"
        )

    record_hashes: dict[str, list[str]] = {}
    for entry in entries:
        result = json.loads((ROOT / entry["result_file"]).read_text(encoding="utf-8"))
        canonical_records = json.dumps(
            result["records"],
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        digest = hashlib.sha256(canonical_records).hexdigest()
        record_hashes.setdefault(digest, []).append(entry["experiment_id"])
    duplicate_groups = sorted(
        sorted(group) for group in record_hashes.values() if len(group) > 1
    )
    expected_duplicate_groups = sorted(
        [
            sorted(
                [
                    "emojimx-single-5@20260814-081647",
                    "emojimx2-s2-emoji@20260814-172649",
                ]
            ),
            sorted(
                [
                    "emojimx2-comp25-s2-emoji@20260814-194141",
                    "emojimx2-comp25-single-5@20260814-185120",
                ]
            ),
        ]
    )
    if len(record_hashes) != 67 or duplicate_groups != expected_duplicate_groups:
        raise SystemExit(
            "69-run duplicate audit changed: expected 67 distinct record arrays "
            f"and {expected_duplicate_groups!r}, found {len(record_hashes)} and "
            f"{duplicate_groups!r}"
        )

    correlation_rows = _unique_correlation_rows()
    shares = [share for _, share, _ in correlation_rows]
    effects = [effect for _, _, effect in correlation_rows]
    pearson = _correlation(shares, effects)
    spearman = _correlation(_rank(shares), _rank(effects))

    return {
        "{{RUN_COUNT}}": "69",
        "{{DISTINCT_RECORD_ARRAYS}}": str(len(record_hashes)),
        "{{REPLY_COUNT}}": "24,810",
        "{{OPEN_RUN_COUNT}}": "58",
        "{{WORDLE_RUN_COUNT}}": "11",
        "{{K25_PEARSON}}": f"{pearson:.02f}",
        "{{K25_SPEARMAN}}": f"{spearman:.02f}",
        "{{K25_N}}": str(len(correlation_rows)),
    }


def assemble() -> Path:
    html = TEMPLATE.read_text(encoding="utf-8")
    for token, value in audit_corpus().items():
        if token not in html:
            raise SystemExit(f"template is missing data token {token}")
        html = html.replace(token, value)

    for token, path in FIGURES.items():
        if token not in html:
            raise SystemExit(f"template is missing figure token {token}")
        if not path.exists():
            raise SystemExit(f"figure is missing: {path}")
        mime = "image/png"
        uri = f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode("ascii")
        html = html.replace(token, uri)

    if "{{JSPACE_SCATTER}}" not in html:
        raise SystemExit("template is missing figure token {{JSPACE_SCATTER}}")
    html = html.replace("{{JSPACE_SCATTER}}", _scatter_uri())

    if "{{LENGTH_NULL}}" not in html:
        raise SystemExit("template is missing figure token {{LENGTH_NULL}}")
    html = html.replace("{{LENGTH_NULL}}", _length_null_uri())

    leftover = sorted(set(re.findall(r"\{\{[A-Z0-9_]+\}\}", html)))
    if leftover:
        raise SystemExit(f"unmapped template tokens: {leftover}")

    HTML_OUT.parent.mkdir(parents=True, exist_ok=True)
    temp = HTML_OUT.with_suffix(".tmp.html")
    temp.write_text(html, encoding="utf-8", newline="\n")
    temp.replace(HTML_OUT)
    return HTML_OUT


def print_pdf(page: Path) -> Path:
    chrome = next((candidate for candidate in CHROME_CANDIDATES if candidate.exists()), None)
    if chrome is None:
        raise SystemExit("Chrome was not found; update CHROME_CANDIDATES")

    PDF_OUT.parent.mkdir(parents=True, exist_ok=True)
    temp_pdf = PDF_OUT.with_suffix(".tmp.pdf")
    if temp_pdf.exists():
        temp_pdf.unlink()
    command = [
        str(chrome),
        "--headless",
        "--disable-gpu",
        "--no-pdf-header-footer",
        "--print-to-pdf-no-header",
        f"--print-to-pdf={temp_pdf}",
        page.resolve().as_uri(),
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0 or not temp_pdf.exists():
        raise SystemExit(
            "Chrome PDF export failed:\n"
            + (result.stdout or "")
            + (result.stderr or "")
        )
    temp_pdf.replace(PDF_OUT)
    return PDF_OUT


if __name__ == "__main__":
    html_path = assemble()
    pdf_path = print_pdf(html_path)
    print(
        f"wrote {html_path.relative_to(ROOT)} "
        f"({html_path.stat().st_size // 1024:,} KB)"
    )
    print(
        f"wrote {pdf_path.relative_to(ROOT)} "
        f"({pdf_path.stat().st_size // 1024:,} KB)"
    )
