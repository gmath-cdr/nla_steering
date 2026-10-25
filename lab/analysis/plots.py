"""Every summary figure and cut, built from lab/results/*.json. Laptop only.

    python analysis/plots.py                 # reads lab/results, writes lab/analysis/figures
    python analysis/plots.py --results DIR --out DIR

Aggregates across prompts and samples (never per-prompt plots) and writes:

  dose_response_<name>.png   per steered run: target probe + coherence vs
                             alpha, random-direction control overlaid when a
                             run with control_of: <name> exists
  census_shapes.png          distribution per probe from census runs; the
                             page that settles every baseline_shape cell
  gradient.png               effect size by tier (the headline figure);
                             low-delta-ratio directions drawn as open circles
  diagnostics.png            reconstruction delta-norm ratio vs achieved
                             effect: a screening diagnostic, not a viability gate
  summary.csv                one row per run: tier, alphas, chosen alpha,
                             effect size, coherence, direction diagnostics

Conventions: a run's TARGET probe is the first entry in its spec's probes
list. Tier comes from the spec's matrix_id (e.g. T1-emoji). When one spec
name has several result files, the newest wins and older ones are listed
as superseded. Effect size is Cliff's delta (scale-free, so every probe
lands on one axis); the reported alpha is the coherence-safe alpha with
the largest absolute effect.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

LAB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB))
from nla_lab.methods import resolve_design, resolve_method  # noqa: E402

# Reference dataviz palette (validated, light mode): series colors follow
# the entity everywhere: blue = the steered run, orange = its random control.
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"
BLUE = "#2a78d6"
ORANGE = "#eb6834"

COHERENCE_PROBE = "unique_word_ratio"
COHERENCE_FLOOR = 0.8  # of the alpha=0 mean; below this an alpha is out of bounds
LOW_RATIO_WARNING = 0.15  # historical screen threshold; not an impossibility gate

plt.rcParams.update({
    "figure.facecolor": SURFACE,
    "axes.facecolor": SURFACE,
    "savefig.facecolor": SURFACE,
    "font.family": "sans-serif",
    "font.sans-serif": ["Segoe UI", "DejaVu Sans"],
    "axes.edgecolor": AXIS,
    "axes.labelcolor": INK_2,
    "xtick.color": MUTED,
    "ytick.color": MUTED,
    "axes.grid": True,
    "grid.color": GRID,
    "grid.linewidth": 0.8,
    "axes.axisbelow": True,
    "figure.dpi": 150,
})


def _despine(ax) -> None:
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)


def load_runs(results_dir: Path) -> list[dict]:
    """Newest result per spec name; older files reported as superseded."""
    by_name: dict[str, tuple[float, dict]] = {}
    for path in sorted(results_dir.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "spec" not in data or "records" not in data:
            continue
        spec = data["spec"]
        method = data.get("method") or resolve_method(spec)
        run = {
            "name": spec["name"],
            "spec": spec,
            "records": data["records"],
            "diagnostics": data.get("direction_diagnostics"),
            "method": method,
            "design": data.get("design") or resolve_design(spec, method),
            "path": path,
        }
        mtime = path.stat().st_mtime
        if spec["name"] in by_name:
            old = by_name[spec["name"]]
            keep, drop = (run, old[1]) if mtime > old[0] else (old[1], run)
            print(f"  superseded: {drop['path'].name} (kept {keep['path'].name})")
            by_name[spec["name"]] = (max(mtime, old[0]), keep)
        else:
            by_name[spec["name"]] = (mtime, run)
    return [run for _, run in by_name.values()]


def probe_values(run: dict, probe: str, alpha: float) -> np.ndarray:
    return np.array([
        r["probes"][probe] for r in run["records"]
        if r["alpha"] == alpha and probe in r["probes"]
    ])


def alphas_of(run: dict) -> list[float]:
    return sorted({r["alpha"] for r in run["records"]})


def target_probe(run: dict) -> str:
    return run["spec"]["probes"][0]


def boot_ci(values: np.ndarray, n: int = 2000) -> tuple[float, float]:
    """95 percent percentile bootstrap CI on the mean, seeded."""
    if len(values) == 0:
        return (np.nan, np.nan)
    rng = np.random.default_rng(0)
    means = rng.choice(values, size=(n, len(values)), replace=True).mean(axis=1)
    return (float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5)))


def cliffs_delta(treated: np.ndarray, baseline: np.ndarray) -> float:
    """Scale-free dominance effect size in [-1, 1]."""
    if len(treated) == 0 or len(baseline) == 0:
        return float("nan")
    diff = treated[:, None] - baseline[None, :]
    return float(((diff > 0).sum() - (diff < 0).sum()) / diff.size)


def coherence_ok(run: dict, alpha: float) -> bool:
    base = probe_values(run, COHERENCE_PROBE, 0.0)
    at = probe_values(run, COHERENCE_PROBE, alpha)
    if len(base) == 0 or len(at) == 0:  # probe not recorded: cannot judge
        return True
    return at.mean() >= COHERENCE_FLOOR * base.mean()


def pick_alpha(run: dict) -> tuple[float, float, bool]:
    """(alpha, cliffs_delta, coherent) with the largest safe |effect|.

    Falls back to the overall-best alpha, flagged incoherent, when no
    nonzero alpha stays above the coherence floor.
    """
    probe = target_probe(run)
    base = probe_values(run, probe, 0.0)
    scored = [
        (a, cliffs_delta(probe_values(run, probe, a), base), coherence_ok(run, a))
        for a in alphas_of(run) if a != 0.0
    ]
    if not scored:
        return (0.0, 0.0, True)
    safe = [s for s in scored if s[2]]
    return max(safe or scored, key=lambda s: abs(s[1]))


def collapse_alpha(run: dict) -> float | None:
    """Smallest alpha where coherence falls below the floor; None if never.

    Magnitude-vs-content discriminator: if this is roughly constant across
    concepts (and matches the random controls), degeneration is about
    injection MAGNITUDE; if it varies systematically by concept, the
    direction's content carries real blame too.
    """
    for a in alphas_of(run):
        if a != 0 and not coherence_ok(run, a):
            return a
    return None


def is_census(run: dict) -> bool:
    return run["method"]["root"]["method"] == "none"


def is_control(run: dict) -> bool:
    return run["method"]["root"]["method"] == "random_isotropic"


def tier_of(run: dict) -> int | None:
    mid = run["spec"].get("matrix_id", "")
    return int(mid[1]) if len(mid) > 1 and mid[0] == "T" and mid[1].isdigit() else None


def _series(ax, run: dict, probe: str, color: str, label: str) -> bool:
    """Draw only recorded probe cells; return whether anything was drawn.

    Matched random-control files do not always contain every treatment target
    probe.  Treating those absent cells as numeric series used to emit NaNs and
    produce misleading empty control lines.
    """
    points = [
        (a, values)
        for a in alphas_of(run)
        if len(values := probe_values(run, probe, a))
    ]
    if not points:
        return False
    xs = [point[0] for point in points]
    means = [point[1].mean() for point in points]
    cis = [boot_ci(point[1]) for point in points]
    ax.fill_between(xs, [c[0] for c in cis], [c[1] for c in cis],
                    color=color, alpha=0.15, linewidth=0)
    ax.plot(xs, means, color=color, linewidth=2, marker="o", markersize=6,
            label=label)
    ax.annotate(label, (xs[-1], means[-1]), xytext=(6, 0),
                textcoords="offset points", color=INK_2, fontsize=8,
                va="center")
    return True


def fig_dose_response(run: dict, control: dict | None, out: Path) -> None:
    probe = target_probe(run)
    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(7, 6), sharex=True, height_ratios=[2, 1])
    _series(ax1, run, probe, BLUE, "steered")
    if control is not None:
        control_drawn = _series(
            ax1, control, probe, ORANGE, "random control"
        )
        if control_drawn:
            ax1.legend(frameon=False, fontsize=8, labelcolor=INK_2)
    a_star, eff, coherent = pick_alpha(run)
    ax1.axvline(a_star, color=AXIS, linewidth=1, linestyle=":")
    note = f"alpha {a_star:g}: Cliff's d = {eff:+.2f}"
    if not coherent:
        note += " (INCOHERENT)"
    ax1.set_title(f"{run['name']}: {probe} vs alpha   [{note}]",
                  color=INK, fontsize=10, loc="left")
    ax1.set_ylabel(probe)
    _series(ax2, run, COHERENCE_PROBE, BLUE, "steered")
    if control is not None:
        _series(ax2, control, COHERENCE_PROBE, ORANGE, "random control")
    base = probe_values(run, COHERENCE_PROBE, 0.0)
    if len(base):
        ax2.axhline(COHERENCE_FLOOR * base.mean(), color=AXIS,
                    linewidth=1, linestyle="--")
    ax2.set_ylabel("coherence\n(unique word ratio)")
    ax2.set_xlabel("alpha")
    for ax in (ax1, ax2):
        _despine(ax)
        ax.margins(x=0.12)
    fig.tight_layout()
    fig.savefig(out)
    plt.close(fig)


def fig_census(census_runs: list[dict], out: Path) -> None:
    records = [r for run in census_runs for r in run["records"]]
    probes = sorted({p for r in records for p in r["probes"]})
    ncols = 4
    nrows = -(-len(probes) // ncols)
    fig, axes = plt.subplots(nrows, ncols, figsize=(11, 2.1 * nrows))
    rng = np.random.default_rng(0)
    for ax, probe in zip(axes.flat, probes):
        vals = np.array([r["probes"][probe] for r in records if probe in r["probes"]])
        jitter = rng.uniform(-0.18, 0.18, len(vals))
        ax.scatter(vals, jitter, s=12, color=BLUE, alpha=0.35, linewidths=0)
        ax.axvline(vals.mean(), color=INK_2, linewidth=1.5)
        ax.set_title(f"{probe}  (mean {vals.mean():.2f})", color=INK_2,
                     fontsize=8, loc="left")
        ax.set_yticks([])
        ax.set_ylim(-0.5, 0.5)
        ax.grid(axis="x")
        _despine(ax)
        ax.spines["left"].set_visible(False)
    for ax in axes.flat[len(probes):]:
        ax.set_visible(False)
    fig.suptitle("Baseline census: what unsteered Qwen does on every probe",
                 color=INK, fontsize=11, x=0.01, ha="left")
    fig.tight_layout(rect=(0, 0, 1, 0.97))
    fig.savefig(out)
    plt.close(fig)


def fig_gradient(rows: list[dict], out: Path) -> None:
    rows = [r for r in rows if r["tier"] is not None]
    if not rows:
        return
    fig, ax = plt.subplots(figsize=(7, 4.5))
    rng = np.random.default_rng(0)
    for r in rows:
        x = r["tier"] + rng.uniform(-0.1, 0.1)
        low_ratio = r["delta_norm_ratio"] is not None and \
            r["delta_norm_ratio"] < LOW_RATIO_WARNING
        ax.scatter(x, r["effect"], s=70, color=BLUE,
                   facecolors="none" if low_ratio else BLUE, linewidths=2, zorder=3)
        label = r["matrix_id"] + (" (low ratio)" if low_ratio else "")
        ax.annotate(label, (x, r["effect"]), xytext=(8, 0),
                    textcoords="offset points", fontsize=8, color=INK_2,
                    va="center")
    ax.axhline(0, color=AXIS, linewidth=1)
    ax.set_xticks([1, 2, 3, 4, 5], ["1\nsurface", "2\nregister",
                  "3\nlexical plan", "4\nstructure", "5\nstance"])
    ax.set_xlim(0.5, 5.5)
    ax.set_ylabel("Cliff's delta at chosen alpha")
    ax.set_title("The complexity gradient: steerability by tier "
                 "(open circle = delta-norm ratio below 0.15)",
                 color=INK, fontsize=10, loc="left")
    _despine(ax)
    fig.tight_layout()
    fig.savefig(out)
    plt.close(fig)


def fig_diagnostics(rows: list[dict], out: Path) -> None:
    rows = [r for r in rows if r["delta_norm_ratio"] is not None]
    if not rows:
        return
    fig, ax = plt.subplots(figsize=(7, 4.5))
    for r in rows:
        ax.scatter(r["delta_norm_ratio"], abs(r["effect"]), s=70, color=BLUE,
                   zorder=3)
        ax.annotate(r["name"], (r["delta_norm_ratio"], abs(r["effect"])),
                    xytext=(8, 0), textcoords="offset points", fontsize=8,
                    color=INK_2, va="center")
    ax.axvline(LOW_RATIO_WARNING, color=AXIS, linewidth=1, linestyle="--")
    ax.annotate("historical low-ratio warning\n(not an impossibility gate)",
                (LOW_RATIO_WARNING, ax.get_ylim()[1]), xytext=(-8, -4),
                textcoords="offset points", fontsize=8, color=MUTED,
                ha="right", va="top")
    ax.set_xlabel("delta-norm ratio between the two reconstructions")
    ax.set_ylabel("|Cliff's delta| at chosen alpha")
    ax.set_title("Does reconstruction separation predict steering success?",
                 color=INK, fontsize=10, loc="left")
    _despine(ax)
    fig.tight_layout()
    fig.savefig(out)
    plt.close(fig)


TIER_COLOR = {1: BLUE, 2: ORANGE, 3: "#1baf7a", 4: "#eda100", 5: "#e87ba4"}


def fig_geometry(rows: list[dict], out: Path) -> None:
    """The reconstruction-pair geometry of every direction: how far apart
    the two sentences landed in the reconstructor's space. Cosine near 1
    with a tiny delta ratio = the reconstructor barely told them apart."""
    rows = [r for r in rows if r["cos_pos_neg"] is not None]
    if not rows:
        return
    fig, ax = plt.subplots(figsize=(7.5, 5))
    for r in rows:
        c = TIER_COLOR.get(r["tier"], MUTED)
        ax.scatter(r["cos_pos_neg"], r["delta_norm_ratio"], s=60, color=c,
                   zorder=3)
        ax.annotate(r["name"].replace("-freeform", ""),
                    (r["cos_pos_neg"], r["delta_norm_ratio"]),
                    xytext=(6, 0), textcoords="offset points", fontsize=6.5,
                    color=INK_2, va="center")
    ax.axhline(LOW_RATIO_WARNING, color=AXIS, linewidth=1, linestyle="--")
    ax.annotate("historical warning", (ax.get_xlim()[0], LOW_RATIO_WARNING),
                xytext=(4, 4), textcoords="offset points", fontsize=7,
                color=MUTED)
    for tier in sorted({r["tier"] for r in rows if r["tier"]}):
        ax.scatter([], [], s=60, color=TIER_COLOR.get(tier, MUTED),
                   label=f"tier {tier}")
    ax.legend(frameon=False, fontsize=8, labelcolor=INK_2)
    ax.set_xlabel("cosine between the two reconstructions AR(+) and AR(-)")
    ax.set_ylabel("delta-norm ratio (direction size)")
    ax.set_title("Direction geometry: how differently the reconstructor "
                 "heard each pair of sentences", color=INK, fontsize=10,
                 loc="left")
    _despine(ax)
    fig.tight_layout()
    fig.savefig(out)
    plt.close(fig)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    lab_dir = Path(__file__).resolve().parents[1]
    ap.add_argument("--results", default=str(lab_dir / "results"))
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent / "figures"))
    args = ap.parse_args()
    results_dir, out_dir = Path(args.results), Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    runs = load_runs(results_dir)
    if not runs:
        print(f"no results JSONs in {results_dir}")
        return
    # control_of may be a string or a list: one shared control run serves
    # every experiment with the same prompts and alphas (its outputs are
    # scored with ALL probes, so each experiment reads its own target)
    controls: dict[str, dict] = {}
    for r in runs:
        if not is_control(r):
            continue
        co = r["spec"].get("control_of") or []
        for parent in [co] if isinstance(co, str) else co:
            controls[parent] = r
    steered = [r for r in runs if not is_census(r) and not is_control(r)]
    census = [r for r in runs if is_census(r)]

    rows = []
    for run in steered:
        a_star, eff, coherent = pick_alpha(run)
        diag = run["diagnostics"] or {}
        rows.append({
            "name": run["name"],
            "matrix_id": run["spec"].get("matrix_id", ""),
            "tier": tier_of(run),
            "target_probe": target_probe(run),
            "n_generations": len(run["records"]),
            "alphas": " ".join(f"{a:g}" for a in alphas_of(run)),
            "alpha_star": a_star,
            "effect": eff,
            "coherent_at_alpha_star": coherent,
            "has_random_control": run["name"] in controls,
            "cos_pos_neg": diag.get("cos_pos_neg"),
            "delta_norm_ratio": diag.get("delta_norm_ratio"),
            "collapse_alpha": collapse_alpha(run),
            "control_collapse_alpha": (
                collapse_alpha(controls[run["name"]])
                if run["name"] in controls else None
            ),
            "result_file": run["path"].name,
        })
        run_dir = out_dir / run["name"]
        run_dir.mkdir(parents=True, exist_ok=True)
        fig_dose_response(run, controls.get(run["name"]),
                          run_dir / "dose_response.png")
    if census:
        fig_census(census, out_dir / "census_shapes.png")
    fig_gradient(rows, out_dir / "gradient.png")
    fig_diagnostics(rows, out_dir / "diagnostics.png")
    fig_geometry(rows, out_dir / "direction_geometry.png")

    summary = out_dir / "summary.csv"
    if rows:
        with summary.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    print(f"wrote {len(rows)} summary rows and figures to {out_dir}")


if __name__ == "__main__":
    main()
