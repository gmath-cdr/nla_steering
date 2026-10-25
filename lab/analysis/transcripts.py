"""Render every results JSON as a readable markdown transcript.

    python analysis/transcripts.py

Writes analysis/transcripts/<name>.md, one file per run: direction
diagnostics (echoes included) at the top, then every raw output grouped
by alpha and prompt with its probe scores inline. This is how you obey
the standing rule: read raw outputs before believing the numbers.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


LAB_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB_DIR))

from nla_lab.methods import (  # noqa: E402
    MethodMetadataError,
    resolve_design,
    resolve_method,
    short_label,
)


def load_run(path: Path) -> dict[str, Any] | None:
    """Load a result produced by ``experiments/run.py``.

    The results directory also contains analysis artifacts such as vector
    metrics and J-space projections. Those are valid JSON, but they are not
    generation runs and therefore have neither an embedded spec nor records.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return None
    if not isinstance(data.get("spec"), dict):
        return None
    if not isinstance(data.get("records"), list):
        return None
    return data


def _method_lines(spec: dict[str, Any]) -> list[str]:
    """Render normalized method/design ancestry without trusting old labels."""
    try:
        method = resolve_method(spec)
        design = resolve_design(spec, method)
    except MethodMetadataError as exc:
        return [f"Method provenance: unresolved ({exc})  ", ""]
    derivation = method["derivation"]
    classification = method["classification"]
    lines = [
        f"Method: {short_label(method)}  ",
        f"Method metadata: {classification['origin']} "
        f"({classification['confidence']} confidence)  ",
        f"Design: {design['intent']}; {design['arm_role']}; "
        f"preregistered={design['preregistered']}  ",
    ]
    if derivation.get("parent_vector_key"):
        lines.append(f"Parent vector: {derivation['parent_vector_key']}  ")
    lines.append("")
    return lines


def render(data: dict[str, Any]) -> str:
    spec = data["spec"]
    lines = [f"# {spec['name']}", ""] + _method_lines(spec)
    if spec.get("matrix_id"):
        lines.append(f"Matrix row: {spec['matrix_id']}  ")
    if isinstance(spec.get("direction"), dict):
        lines += [f"direction +: {spec['direction']['positive']}  ",
                  f"direction -: {spec['direction']['negative']}  ", ""]
    elif spec.get("direction") == "random":
        lines += [f"direction: RANDOM unit vector (control of "
                  f"{spec.get('control_of', '?')})", ""]
    diag = data.get("direction_diagnostics")
    if diag and "cos_pos_neg" in diag:
        lines += ["## Direction diagnostics", "",
                  f"- cos(AR+, AR-): {diag['cos_pos_neg']:.3f}",
                  f"- delta norm ratio: {diag['delta_norm_ratio']:.3f}"]
        for k in ("echo_positive", "echo_negative"):
            if k in diag:
                lines.append(f"- {k}: {diag[k]}")
        lines.append("")
    by_alpha: dict[float, list[dict]] = {}
    primary_field = spec.get("study", {}).get("primary_metric")
    for r in data["records"]:
        by_alpha.setdefault(r["alpha"], []).append(r)
    for alpha in sorted(by_alpha):
        lines += [f"## alpha = {alpha:g}", ""]
        for r in by_alpha[alpha]:
            score_items = list(r["probes"].items())
            target = r.get("target_metrics") or {}
            for key in ("target_count", "target_present", "target_rate_per_100_words"):
                if key in target:
                    score_items.append((key, target[key]))
            for key in (
                primary_field,
                "supported_attribution_count",
                "supported_attribution_rate_per_100_words",
                "generic_citation_marker_count",
                "generic_citation_marker_rate_per_100_words",
            ):
                if key and key in target and key not in {name for name, _ in score_items}:
                    score_items.append((str(key), target[key]))
            probes = "  ".join(
                f"{k}={v:g}" if isinstance(v, (int, float)) else f"{k}={v}"
                for k, v in score_items
            )
            prompt_label = r.get("prompt_id", r["prompt"])
            split = f"; split {r['prompt_split']}" if r.get("prompt_split") else ""
            lines += [f"**{prompt_label}: {r['prompt']}**  "
                      f"(sample {r['sample']}{split};  {probes})", "",
                      "> " + r["text"].replace("\n", "\n> "), ""]
    return "\n".join(lines)


def main() -> None:
    out_dir = Path(__file__).resolve().parent / "transcripts"
    out_dir.mkdir(exist_ok=True)
    for path in sorted((LAB_DIR / "results").glob("*.json")):
        data = load_run(path)
        if data is None:
            print(f"skipped {path.name}: not a generation-run result")
            continue
        out = out_dir / (path.stem + ".md")
        out.write_text(render(data), encoding="utf-8")
        print(f"wrote {out.name}")


if __name__ == "__main__":
    main()
