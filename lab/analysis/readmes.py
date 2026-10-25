"""Write a plain-words README.md into every figures/<experiment>/ folder.

    python analysis/readmes.py

Each README answers "what experiment is this?" for someone browsing the
folder cold: what was steered and in which direction, the exact sentences
the reconstructor ingested, the task prompts, the settings, and what each
figure in the folder shows. GitHub renders it automatically when the
folder is opened. Content comes from the results JSON (what actually
ran) plus the spec file's leading comment block (the why).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

LAB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB))
from nla_lab.methods import resolve_design, resolve_method, short_label  # noqa: E402


def spec_comments(lab_dir: Path) -> dict[str, str]:
    """name -> the spec file's leading # comment block, uncommented."""
    out = {}
    for f in (lab_dir / "experiments" / "specs").glob("*.yaml"):
        lines = []
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.startswith("#"):
                lines.append(line.lstrip("#").strip())
            elif line.strip():
                break
        try:
            name = yaml.safe_load(f.read_text(encoding="utf-8"))["name"]
            out[name] = " ".join(lines)
        except Exception:
            pass
    return out


FIG_BLURBS = {
    "dose_response.png": "target probe (top) and text coherence (bottom) "
    "as steering strength alpha increases; an orange matched-random line is "
    "shown only when that control recorded the same probe",
    "answers_by_alpha.png": "the full answer histogram at each alpha "
    "(choice tasks); blue bar = the baseline modal answer",
    "vector_geometry.png": "where this experiment's two reconstruction "
    "vectors and its prompt state sit on the shared 2D vector map, with "
    "a reading guide underneath",
}


def render(data: dict, why: str, run_dir: Path) -> str:
    spec = data["spec"]
    direction = spec.get("direction")
    method = data.get("method") or resolve_method(spec)
    design = data.get("design") or resolve_design(spec, method)
    derivation = method["derivation"]
    lines = [f"# {spec['name']}", ""]
    if why:
        lines += [why, ""]
    lines.append("## What was steered")
    lines.append("")
    lines += [
        f"- **Registered ancestry**: `{short_label(method)}`",
        f"- **Vector view**: `{method['vector_view']}`",
        f"- **Intervention**: `{method['intervention']['kind']}` / "
        f"`{method['intervention']['token_scope']}` / "
        f"`{method['intervention']['scaling_rule']}`",
        f"- **Evidence intent**: `{design['intent']}` "
        f"(metadata: `{design['classification_origin']}`)",
    ]
    if isinstance(direction, dict):
        lines += [
            f"- **Steering TOWARD** (the reconstructor ingested exactly "
            f"this): \"{direction['positive']}\"",
            f"- **Steering AWAY from**: \"{direction['negative']}\"",
        ]
    elif direction == "random":
        lines.append(
            "- **Random control**: a meaningless random unit vector "
            f"(seed {spec.get('direction_seed', 0)}) injected at matched "
            "strengths. If a probe moves here too, movement is generic "
            "perturbation, not steering content."
        )
    elif spec.get("direction_file"):
        lines.append(f"- **Saved vector**: `{spec['direction_file']}`")
        if derivation["operator"] != "none":
            lines.append(
                f"- **Derivation**: `{derivation['operator']}` → "
                f"`{derivation['output_role']}` from parent key "
                f"`{derivation['parent_vector_key']}`"
            )
    else:
        lines.append("- **No steering**: unsteered baseline / census run.")
    if spec.get("matrix_id"):
        lines.append(f"- Experiment matrix row: {spec['matrix_id']}")
    lines += ["", "## Task prompts the model answered", ""]
    for prompt in spec.get("prompts", []):
        if isinstance(prompt, dict):
            lines.append(
                f"- `{prompt.get('id', 'unidentified')}` "
                f"[{prompt.get('split', 'unsplit')}]: {prompt.get('text', '')}"
            )
        else:
            lines.append(f"- {prompt}")
    lines += ["", "## Settings", ""]
    lines.append(
        f"- alphas {spec.get('alphas', [0])}, "
        f"{spec.get('samples_per_prompt', 3)} samples per prompt per "
        f"alpha, mode {spec.get('mode', 'n/a')}, temperature "
        f"{spec.get('temperature', 0.7)}, max {spec.get('max_new_tokens', 200)} "
        "new tokens"
    )
    diag = data.get("direction_diagnostics") or {}
    if "delta_norm_ratio" in diag:
        lines.append(
            f"- direction diagnostics: cos(AR+, AR-) = "
            f"{diag['cos_pos_neg']:.3f}, delta-norm ratio = "
            f"{diag['delta_norm_ratio']:.3f} (under ~0.15 triggers a historical "
            "warning, not an impossibility conclusion)"
        )
    figs = [f.name for f in sorted(run_dir.glob("*.png"))]
    if figs:
        lines += ["", "## Figures in this folder", ""]
        for f in figs:
            lines.append(f"- `{f}`: {FIG_BLURBS.get(f, '')}")
    lines += ["", "Raw outputs: `lab/results/`; readable transcripts: "
              "`lab/analysis/transcripts/`.", ""]
    return "\n".join(lines)


def main() -> None:
    lab_dir = Path(__file__).resolve().parents[1]
    fig_dir = Path(__file__).resolve().parent / "figures"
    why = spec_comments(lab_dir)
    newest: dict[str, Path] = {}
    for p in sorted((lab_dir / "results").glob("*.json")):
        data = json.loads(p.read_text(encoding="utf-8"))
        if "spec" in data:
            newest[data["spec"]["name"]] = p
    n = 0
    for name, path in newest.items():
        run_dir = fig_dir / name
        if not run_dir.is_dir():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        (run_dir / "README.md").write_text(
            render(data, why.get(name, ""), run_dir), encoding="utf-8")
        n += 1
    print(f"wrote {n} experiment READMEs under {fig_dir}")


if __name__ == "__main__":
    main()
