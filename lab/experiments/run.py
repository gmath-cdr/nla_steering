"""Run a steering experiment from a small spec file. No Python editing needed.

    python experiments/run.py experiments/specs/emoji.yaml

An experiment includes these behavioral decisions:

  1. direction  an inline pair, random control, saved vector, or no vector.
                Inline pairs are specifically AR(positive) - AR(negative),
                not AV-edit-AR constructions.
  2. prompts    the questions to test on.
  3. alphas     the strength ladder. Include 0 unless a prospective spec is
                cryptographically linked to its separately generated baseline.
  4. probes     which deterministic counters to score with (see
                 nla_lab/probes.py for the full list).

Prospective studies may use structured prompts (``{id, text, split}``) and one
parameterized ``target_metric`` (exact Unicode word, registered probe, or
citation markers). The runner records both prompt identity and target/coherence
metrics without changing legacy string-prompt specs.

New specs should also include explicit ``method`` and ``design`` blocks using
steering-taxonomy/v1. Legacy specs are conservatively backfilled and labelled
as inferred; use --require-explicit-method for confirmatory work.

Everything else (temperature, samples, mode) has sensible defaults you can
override in the spec. Results are printed as a table and written in full,
raw outputs included, to lab/results/<name>_<timestamp>.json.

Same seeds are reused across alphas, so each sample at alpha=1 is directly
comparable to the same-numbered sample at alpha=0 (a paired comparison,
matching the research doc's within-prompt design).

A steered run loads base model, reconstructor, and verbalizer (~41 GB):
before any sweep it logs direction diagnostics (cosine and delta-norm ratio
between the two reconstructions, plus the echo of each sentence). These are
screening signals, not hard certificates that a direction can or cannot steer.
Pass --no-echo to skip the verbalizer and log math-only diagnostics.
A census run needs only the base model.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import importlib.metadata
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import torch
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nla_lab import NLASession, probes as probelib  # noqa: E402
from nla_lab.methods import resolve_design, resolve_method, short_label  # noqa: E402
from nla_lab.study import (  # noqa: E402
    canonical_text_file_sha256,
    normalize_prompts,
    validate_spec_integrity,
)
from nla_lab.target_metrics import (  # noqa: E402
    score_target,
    validate_target_metric,
)

# Residual-stream width, from the active model config (NLA_MODEL env var);
# used for the random-direction control, which needs no reconstructor.
from nla_lab.session import (  # noqa: E402
    HIDDEN as ACTIVE_HIDDEN,
    LAYER as ACTIVE_LAYER,
    MODEL_DIRS,
    NLA_MODEL,
    models_dir,
)


_SESSION_CACHE: dict[str, object] = {}


def _session(
    *, models_root: Path, load: tuple[str, ...], reuse: bool
) -> NLASession:
    """Load one session or reuse a compatible superset inside a batch process."""

    if not reuse:
        return NLASession(models_root=str(models_root), load=load)
    requested = set(load)
    cached = _SESSION_CACHE.get("session")
    cached_root = _SESSION_CACHE.get("models_root")
    cached_roles = set(_SESSION_CACHE.get("roles", set()))
    if (
        isinstance(cached, NLASession)
        and cached_root == str(models_root)
        and requested.issubset(cached_roles)
    ):
        print(f"  reusing loaded model session ({', '.join(sorted(cached_roles))})")
        return cached
    if cached is not None:
        _SESSION_CACHE.clear()
        del cached
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    ordered_roles = tuple(
        role for role in ("base", "av", "ar") if role in requested
    )
    session = NLASession(models_root=str(models_root), load=ordered_roles)
    _SESSION_CACHE.update({
        "session": session,
        "models_root": str(models_root),
        "roles": set(ordered_roles),
    })
    return session


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_unit_f32(value: object) -> np.ndarray:
    """Return the stable NumPy-float32 unit vector used for file directions."""
    array = np.asarray(value, dtype=np.float32).reshape(-1)
    norm = float(np.linalg.norm(array))
    if not np.isfinite(norm) or norm <= 1e-12:
        raise ValueError("cannot normalize a non-finite or zero direction")
    return array / norm


def _tensor_f32_sha256(tensor: torch.Tensor) -> str:
    """Hash a tensor in a documented, device-independent representation."""
    value = tensor.detach().float().cpu().contiguous()
    digest = hashlib.sha256()
    digest.update(b"steering-tensor-f32-le/v1\0")
    digest.update(json.dumps(list(value.shape), separators=(",", ":")).encode())
    digest.update(b"\0")
    digest.update(value.numpy().astype("<f4", copy=False).tobytes(order="C"))
    return digest.hexdigest()


def _package_versions() -> dict[str, str | None]:
    versions: dict[str, str | None] = {}
    for package in ("torch", "transformers", "numpy", "scipy", "PyYAML"):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
    return versions


def _git_state(repo: Path) -> dict[str, str | bool | None]:
    def run(*args: str) -> str | None:
        result = subprocess.run(
            ["git", *args], cwd=repo, text=True, capture_output=True,
            check=False, timeout=10,
        )
        return result.stdout.strip() if result.returncode == 0 else None

    commit = run("rev-parse", "HEAD")
    status = run("status", "--porcelain")
    return {
        "commit": commit,
        "dirty": bool(status) if status is not None else None,
    }


def _checkpoint_markers(root: Path) -> dict[str, dict[str, str]]:
    """Hash small identity/config files without re-hashing multi-GB weights."""
    markers = (
        "config.json",
        "generation_config.json",
        "model.safetensors.index.json",
        "tokenizer.json",
        "tokenizer_config.json",
        "tokenizer.model",
        "special_tokens_map.json",
        "added_tokens.json",
        "chat_template.jinja",
        "nla_meta.yaml",
    )
    output: dict[str, dict[str, str]] = {}
    for role, dirname in MODEL_DIRS.items():
        checkpoint = root / dirname
        found = {
            name: _sha256_file(checkpoint / name)
            for name in markers
            if (checkpoint / name).is_file()
        }
        output[role] = found
    return output


def main(argv: list[str] | None = None) -> None:
    run_started_epoch = time.time()
    run_started_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(run_started_epoch))
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("spec", help="path to a YAML spec (see experiments/specs/)")
    ap.add_argument("--models-root", default=None,
                    help="override NLA_MODELS_DIR if needed")
    ap.add_argument(
        "--output-dir", default=None,
        help="write result JSON outside lab/results (required by frozen studies)",
    )
    ap.add_argument("--no-echo", action="store_true",
                    help="skip the verbalizer: direction diagnostics keep the "
                         "reconstruction math but lose the echo texts")
    ap.add_argument(
        "--require-explicit-method",
        action="store_true",
        help="reject legacy specs without steering-taxonomy/v1 metadata",
    )
    ap.add_argument(
        "--reuse-session",
        action="store_true",
        help="reuse a compatible NLASession when run.py is called repeatedly "
             "inside experiments/run_specs.py",
    )
    args = ap.parse_args(argv)

    spec = yaml.safe_load(Path(args.spec).read_text(encoding="utf-8"))
    execution_guard = spec.get("execution_guard", {})
    if not isinstance(execution_guard, dict):
        raise ValueError("execution_guard must be a mapping")
    prospective_schema = spec.get("study", {}).get("schema") in {
        "axbench-derived-run/v1",
        "emoji-method-comparison-run/v1",
        "axb44-run/v1",
    }
    validate_spec_integrity(
        spec,
        required=bool(execution_guard.get("require_spec_integrity")) or prospective_schema,
    )
    if prospective_schema:
        required_guards = {
            "require_explicit_model_env",
            "require_clean_git",
            "require_spec_integrity",
            "require_external_output_dir",
        }
        missing_guards = sorted(
            key for key in required_guards if execution_guard.get(key) is not True
        )
        if missing_guards:
            raise ValueError(
                f"prospective run is missing mandatory guards: {missing_guards}"
            )
        if not isinstance(spec.get("expected_code_sha256"), dict) or not spec["expected_code_sha256"]:
            raise ValueError("prospective run requires non-empty expected_code_sha256")
        if not isinstance(spec.get("expected_runtime"), dict):
            raise ValueError("prospective run requires expected_runtime")
    if args.require_explicit_method and "method" not in spec:
        raise ValueError(
            "this run requires an explicit method block; see "
            "docs/METHOD_TAXONOMY.md"
        )
    method = resolve_method(spec)
    design = resolve_design(spec, method)
    name = spec["name"]
    repo_root = Path(__file__).resolve().parents[2]
    active_models_root = (
        Path(args.models_root) if args.models_root else models_dir()
    ).resolve()
    out_dir = (
        Path(args.output_dir).resolve()
        if args.output_dir else Path(__file__).resolve().parents[1] / "results"
    )
    if execution_guard.get("require_explicit_model_env") and "NLA_MODEL" not in os.environ:
        raise ValueError("this study requires NLA_MODEL to be set explicitly")
    if execution_guard.get("require_clean_git"):
        state = _git_state(repo_root)
        if state["dirty"] is not False:
            raise ValueError(
                "confirmatory generation requires a clean Git worktree; "
                f"observed dirty={state['dirty']!r}"
            )
    if execution_guard.get("require_external_output_dir"):
        try:
            out_dir.resolve().relative_to(repo_root.resolve())
        except ValueError:
            pass
        else:
            raise ValueError(
                "this frozen study requires --output-dir outside the Git worktree"
            )
    expected_code = spec.get("expected_code_sha256")
    if expected_code is not None:
        if not isinstance(expected_code, dict) or not expected_code:
            raise ValueError("expected_code_sha256 must be a non-empty mapping")
        for relative, expected in expected_code.items():
            path = repo_root / str(relative)
            if not path.is_file() or canonical_text_file_sha256(path) != expected:
                raise ValueError(f"live code hash mismatch for {relative}")
    # Fail before loading a model (and certainly before paid generation) if
    # the destination cannot be created or written.
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=out_dir, prefix=".nla-write-test-"):
        pass

    expected_runtime = spec.get("expected_runtime")
    if expected_runtime is not None:
        if not isinstance(expected_runtime, dict):
            raise ValueError("expected_runtime must be a mapping")
        observed_runtime = {
            "model_key": NLA_MODEL,
            "layer": ACTIVE_LAYER,
            "hidden_width": ACTIVE_HIDDEN,
        }
        for key, observed in observed_runtime.items():
            if key not in expected_runtime:
                raise ValueError(f"expected_runtime missing {key!r}")
            if expected_runtime[key] != observed:
                raise ValueError(
                    f"runtime mismatch for {key}: expected "
                    f"{expected_runtime[key]!r}, observed {observed!r}"
                )
    expected_base_markers = spec.get("expected_base_checkpoint_marker_sha256")
    if expected_base_markers is not None:
        if not isinstance(expected_base_markers, dict) or not expected_base_markers:
            raise ValueError("expected base checkpoint markers must be a non-empty mapping")
        observed_base_markers = _checkpoint_markers(active_models_root)["base"]
        if any(
            observed_base_markers.get(name) != digest
            for name, digest in expected_base_markers.items()
        ):
            raise ValueError(
                "base checkpoint/tokenizer markers do not match the atom build receipt"
            )
    expected_packages = spec.get("expected_package_versions")
    if expected_packages is not None:
        if not isinstance(expected_packages, dict) or not expected_packages:
            raise ValueError("expected_package_versions must be a non-empty mapping")
        if _package_versions() != expected_packages:
            raise ValueError("runtime package versions differ from calibration")
    # A spec WITHOUT a direction is a baseline census: unsteered generations
    # only, measuring what Qwen currently favors on every probe. Run this
    # before trusting any baseline_shape assumption in the experiment matrix.
    # direction_file: "path.npz#key" -> steer with a saved vector (e.g. a
    # lens-split piece); unit-normalized on load, needs no reconstructor.
    file_dir: str | None = spec.get("direction_file")
    steered = bool(spec.get("direction")) or bool(file_dir)
    # direction: random -> a seeded random unit vector at matched alphas,
    # the control that shows a probe rise is about the direction's CONTENT,
    # not about perturbing layer 20 with any large vector.
    random_dir = spec.get("direction") == "random"
    prompts = normalize_prompts(spec["prompts"])
    alphas: list[float] = spec.get("alphas", [0, 0.5, 1, 2]) if steered else [0]
    mode: str = spec.get("mode", "all")
    # for mode=position: which prompt index to steer. -1 (default) is the
    # last prompt token, the assistant header, the paper's setup.
    position: int = int(spec.get("position", -1))
    n_samples: int = spec.get("samples_per_prompt", 3)
    # seed_offset: fresh seeds for a rerun (e.g. 15 -> seeds 15..29), so
    # pooling with an earlier wave doubles n instead of duplicating it
    seed_offset: int = int(spec.get("seed_offset", 0))
    seed_scheme: str = str(spec.get("seed_scheme", "shared_sample_index_v1"))
    if seed_scheme not in {"shared_sample_index_v1", "prompt_offset_v1"}:
        raise ValueError(f"unknown seed_scheme {seed_scheme!r}")
    temperature: float = spec.get("temperature", 0.7)
    max_new_tokens: int = spec.get("max_new_tokens", 200)
    probe_names: list[str] = spec.get("probes", list(probelib.ALL_PROBES))
    for p in probe_names:
        if p not in probelib.ALL_PROBES:
            raise ValueError(
                f"unknown probe {p!r}; available: {list(probelib.ALL_PROBES)}"
            )
    linked_baseline = spec.get("linked_baseline_spec_sha256")
    prospective_linked_baseline = (
        spec.get("study", {}).get("schema") == "axb44-run/v1"
        and isinstance(linked_baseline, str)
        and len(linked_baseline) == 64
        and all(char in "0123456789abcdef" for char in linked_baseline)
    )
    if 0 not in [float(a) for a in alphas] and not prospective_linked_baseline:
        raise ValueError(
            "alphas must include 0 unless an axb44 spec binds a sealed baseline spec"
        )
    for prompt_entry in prompts:
        explicit_seeds = prompt_entry.get("generation_seeds")
        if explicit_seeds is None:
            continue
        if (
            not isinstance(explicit_seeds, list)
            or len(explicit_seeds) != n_samples
            or any(not isinstance(seed, int) or seed < 0 for seed in explicit_seeds)
            or len(set(explicit_seeds)) != len(explicit_seeds)
        ):
            raise ValueError(
                f"prompt {prompt_entry['id']!r} generation_seeds must contain "
                f"{n_samples} distinct nonnegative integers"
            )
    target_metric = (
        validate_target_metric(spec["target_metric"])
        if "target_metric" in spec else None
    )
    primary_metric_field = str(
        spec.get("study", {}).get("primary_metric", "target_rate_per_100_words")
    )
    if target_metric is not None:
        preview_target = score_target("", target_metric, prompt=prompts[0])
        if primary_metric_field not in preview_target:
            raise ValueError(
                f"primary metric {primary_metric_field!r} is not emitted by target_metric"
            )
    raw_monitors = spec.get("monitor_metrics", {})
    if not isinstance(raw_monitors, dict):
        raise ValueError("monitor_metrics must be a mapping")
    monitor_metrics = {
        str(key): validate_target_metric(value)
        for key, value in raw_monitors.items()
    }
    planned_records = len(prompts) * len(alphas) * n_samples
    if "expected_records" in spec and int(spec["expected_records"]) != planned_records:
        raise ValueError(
            f"expected_records={spec['expected_records']} conflicts with the "
            f"materialized matrix cardinality {planned_records}"
        )

    print(f"experiment: {name}")
    print(f"  method: {short_label(method)}")
    print(f"  method metadata: {method['classification']['origin']}")
    if method["classification"]["origin"] != "explicit_spec":
        print("  WARNING: legacy method backfill; use an explicit method block "
              "for new/confirmatory work")
    if random_dir:
        print(f"  direction: random unit vector (control), "
              f"seed {int(spec.get('direction_seed', 0))}")
    elif file_dir:
        print(f"  direction: from file {file_dir}")
    elif steered:
        positive = spec["direction"]["positive"]
        negative = spec["direction"]["negative"]
        print(f"  direction +: {positive}")
        print(f"  direction -: {negative}")
    else:
        print("  baseline census: no steering, alpha=0 only")
    print(f"  {len(prompts)} prompts x {len(alphas)} alphas x {n_samples} samples "
          f"= {planned_records} generations\n")

    # census and random-control runs need only the base model; sentence
    # directions also need the reconstructor, plus the verbalizer for echoes
    if steered and not random_dir and not file_dir:
        load = ("base", "ar") if args.no_echo else ("base", "av", "ar")
    else:
        load = ("base",)
    lab = _session(
        models_root=active_models_root,
        load=load,
        reuse=bool(args.reuse_session),
    )
    expected_atom_runtime = spec.get("expected_atom_runtime")
    if expected_atom_runtime is not None:
        if not isinstance(expected_atom_runtime, dict):
            raise ValueError("expected_atom_runtime must be a mapping")
        token = expected_atom_runtime.get("token", {})
        variant = token.get("chosen_variant")
        resolution = token.get("resolutions", {}).get(variant, {})
        surface = resolution.get("surface")
        token_id = token.get("token_id")
        if not isinstance(surface, str) or not isinstance(token_id, int):
            raise ValueError("atom token receipt lacks its chosen surface/token ID")
        encoded = list(lab.tok.encode(surface, add_special_tokens=False))
        if encoded != [token_id]:
            raise ValueError(
                f"live tokenizer resolves atom surface to {encoded}, expected {[token_id]}"
            )
        head = lab.base.get_output_embeddings()
        if head is None:
            head = lab.base.get_input_embeddings()
        row_hash = _tensor_f32_sha256(head.weight[token_id].detach().float().cpu())
        if row_hash != expected_atom_runtime.get("unembedding_row_f32_sha256"):
            raise ValueError("live unembedding row does not match the atom build receipt")
        final_norm = None
        for attr_path in (
            "model.norm", "model.language_model.norm", "language_model.model.norm"
        ):
            candidate = lab.base
            try:
                for attr in attr_path.split("."):
                    candidate = getattr(candidate, attr)
                final_norm = candidate
                break
            except AttributeError:
                continue
        if final_norm is None:
            raise ValueError("could not locate the live model's final norm")
        norm_device = next(final_norm.parameters()).device
        with torch.no_grad():
            norm_response = final_norm(
                torch.ones((1, ACTIVE_HIDDEN), device=norm_device, dtype=torch.float32)
            )
        norm_hash = _tensor_f32_sha256(
            norm_response.detach().float().reshape(-1).cpu()
        )
        if norm_hash != expected_atom_runtime.get("effective_norm_response_f32_sha256"):
            raise ValueError("live final-norm response does not match the atom build receipt")
    if random_dir:
        g = torch.Generator().manual_seed(int(spec.get("direction_seed", 0)))
        d = torch.randn(ACTIVE_HIDDEN, generator=g)
        delta = d / d.norm()
    elif file_dir:
        import numpy as np
        path, key = file_dir.rsplit("#", 1)
        raw = np.load(Path(__file__).resolve().parents[1] / path)[key]
        d = torch.from_numpy(raw.astype("float32")).flatten()
        if d.numel() != ACTIVE_HIDDEN:
            raise ValueError(
                f"{file_dir}: got {d.numel()} numbers, active space expects {ACTIVE_HIDDEN}"
            )
        # File-vector receipts use the shared NumPy float32 canonical unit
        # representation.  Reuse those exact bytes for intervention identity;
        # torch.norm and numpy.linalg.norm can differ by a few float32 ulps.
        delta = torch.from_numpy(canonical_unit_f32(raw))
    elif steered:
        delta = lab.direction(positive, negative)
    else:
        delta = None

    direction_integrity = None
    if delta is not None:
        direction_integrity = {
            "schema": "steering-tensor-f32-le/v1",
            "unit_direction_sha256": _tensor_f32_sha256(delta),
            "numel": int(delta.numel()),
            "l2_norm": float(delta.detach().float().norm().cpu()),
        }
        if file_dir:
            source_path = Path(__file__).resolve().parents[1] / path
            source_tensor = torch.from_numpy(raw.astype("float32")).flatten()
            direction_integrity.update({
                "source_archive_sha256": _sha256_file(source_path),
                "source_member_key": key,
                "source_member_f32_sha256": _tensor_f32_sha256(source_tensor),
            })
        expected_integrity = spec.get("expected_direction_integrity", {})
        if not isinstance(expected_integrity, dict):
            raise ValueError("expected_direction_integrity must be a mapping")
        for key, expected in expected_integrity.items():
            if key not in direction_integrity:
                raise ValueError(f"cannot validate unknown direction integrity field {key!r}")
            if direction_integrity[key] != expected:
                raise ValueError(
                    f"direction integrity mismatch for {key}: expected "
                    f"{expected!r}, observed {direction_integrity[key]!r}"
                )

    # Direction diagnostics: useful preflight evidence, logged with every run.
    # Low delta ratio or an echo that drops the target raises risk but is not a
    # hard impossibility result (the historical exact-HOUSE counterexample has
    # both a low ratio and an echo that drops HOUSE).
    diagnostics = None
    if random_dir:
        diagnostics = {
            "type": "random_control",
            "seed": int(spec.get("direction_seed", 0)),
        }
    elif file_dir:
        diagnostics = {
            "type": "file_direction",
            "file": file_dir,
            "prenorm_length": float(
                torch.from_numpy(raw.astype("float32")).norm()),
        }
    elif steered:
        ar_pos = lab.reconstruct(positive).float().flatten()
        ar_neg = lab.reconstruct(negative).float().flatten()
        cos = float((ar_pos @ ar_neg) / (ar_pos.norm() * ar_neg.norm()))
        delta_ratio = float(
            (ar_pos - ar_neg).norm() / ((ar_pos.norm() + ar_neg.norm()) / 2)
        )
        diagnostics = {
            "cos_pos_neg": cos,
            "delta_norm_ratio": delta_ratio,
            "norm_pos": float(ar_pos.norm()),
            "norm_neg": float(ar_neg.norm()),
        }
        if not args.no_echo:
            diagnostics["echo_positive"] = lab.echo(positive)
            diagnostics["echo_negative"] = lab.echo(negative)
        print("  direction diagnostics (read BEFORE trusting the sweep):")
        print(f"    cos(AR+, AR-) = {cos:.3f}   delta norm ratio = {delta_ratio:.3f}")
        if "echo_positive" in diagnostics:
            print(f"    echo +: {diagnostics['echo_positive']}")
            print(f"    echo -: {diagnostics['echo_negative']}")
        if delta_ratio < 0.15 or cos > 0.98:
            print("    WARNING: the reconstructor weakly distinguishes these "
                  "sentences. Treat this as risk evidence, inspect the J-lens "
                  "and matched controls, and do not infer impossibility from "
                  "the echo alone.")
        print()

    records: list[dict] = []
    for alpha in alphas:
        t0 = time.time()
        for prompt_index, prompt_entry in enumerate(prompts):
            prompt = prompt_entry["text"]
            for s in range(n_samples):
                explicit_seeds = prompt_entry.get("generation_seeds")
                seed = (
                    explicit_seeds[s] if explicit_seeds is not None else
                    seed_offset + prompt_index * n_samples + s
                    if seed_scheme == "prompt_offset_v1" else
                    seed_offset + s
                )
                generated = lab.generate(
                    prompt,
                    direction=delta,
                    alpha=float(alpha),
                    mode=mode,
                    position=position if mode == "position" else None,
                    temperature=temperature,
                    max_new_tokens=max_new_tokens,
                    seed=seed,
                    return_metadata=True,
                )
                text, generation = generated
                target_model_perplexity = (
                    lab.output_text_perplexity(text)
                    if spec.get("record_target_model_perplexity") is True else None
                )
                record = {
                    "alpha": float(alpha),
                    "prompt": prompt,
                    "carrier_prompt": prompt_entry.get("carrier_text", prompt),
                    "prompt_id": prompt_entry["id"],
                    "prompt_split": prompt_entry.get("split"),
                    "prompt_track": prompt_entry.get("track"),
                    "concept_id": prompt_entry.get("concept_id"),
                    "concept_label": prompt_entry.get("concept_label"),
                    "sample_index": s,
                    "sample": seed,
                    "text": text,
                    "generation": generation,
                    "target_model_output_perplexity": target_model_perplexity,
                    "probes": {p: probelib.ALL_PROBES[p](text) for p in probe_names},
                }
                if target_metric is not None:
                    record["target_metrics"] = score_target(
                        text, target_metric, prompt=prompt_entry
                    )
                if monitor_metrics:
                    record["monitor_metrics"] = {
                        key: score_target(text, metric, prompt=prompt_entry)
                        for key, metric in monitor_metrics.items()
                    }
                records.append(record)
        print(f"  alpha={alpha:g} done in {time.time() - t0:.0f}s")
    if len(records) != planned_records:
        raise RuntimeError(
            f"generation cardinality mismatch: expected {planned_records}, "
            f"recorded {len(records)}"
        )

    # summary: mean of each probe per alpha
    print(f"\n{'alpha':>6}  " + "  ".join(f"{p:>18}" for p in probe_names))
    for alpha in alphas:
        rows = [r for r in records if r["alpha"] == float(alpha)]
        means = {
            p: sum(r["probes"][p] for r in rows) / len(rows) for p in probe_names
        }
        print(f"{alpha:>6g}  " + "  ".join(f"{means[p]:>18.2f}" for p in probe_names))
    if target_metric is not None:
        print("\nparameterized target metric (mean by alpha)")
        print(f"{'alpha':>6}  {'count':>10}  {'presence':>10}  {'rate/100w':>12}")
        for alpha in alphas:
            rows = [r for r in records if r["alpha"] == float(alpha)]
            print(
                f"{alpha:>6g}  "
                f"{sum(float(r['target_metrics']['target_count']) for r in rows) / len(rows):>10.2f}  "
                f"{sum(float(r['target_metrics']['target_present']) for r in rows) / len(rows):>10.2f}  "
                f"{sum(float(r['target_metrics']['target_rate_per_100_words']) for r in rows) / len(rows):>12.2f}"
            )
        print(f"primary endpoint: {primary_metric_field}")
        for alpha in alphas:
            rows = [r for r in records if r["alpha"] == float(alpha)]
            mean_primary = sum(
                float(r["target_metrics"][primary_metric_field]) for r in rows
            ) / len(rows)
            print(f"  alpha={alpha:g}: {mean_primary:.4f}")

    out_path = out_dir / f"{name}_{time.strftime('%Y%m%d-%H%M%S')}.json"
    runtime = {
        "model_key": NLA_MODEL,
        "layer": ACTIVE_LAYER,
        "hidden_width": ACTIVE_HIDDEN,
        "base_model_dir_name": MODEL_DIRS["base"],
        "model_dirs": {
            role: str((active_models_root / dirname).resolve())
            for role, dirname in MODEL_DIRS.items()
        },
        "checkpoint_marker_sha256": _checkpoint_markers(active_models_root),
        "nla_model_env_explicit": "NLA_MODEL" in os.environ,
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "package_versions": _package_versions(),
        "gpu": (
            {
                "device_index": int(torch.cuda.current_device()),
                "name": torch.cuda.get_device_name(torch.cuda.current_device()),
                "compute_capability": list(torch.cuda.get_device_capability()),
                "total_memory_bytes": int(
                    torch.cuda.get_device_properties(torch.cuda.current_device()).total_memory
                ),
                "torch_cuda_version": torch.version.cuda,
                "device_count": int(torch.cuda.device_count()),
            }
            if torch.cuda.is_available() else None
        ),
        "git": _git_state(repo_root),
        "result_output_dir": str(out_dir.resolve()),
        "run_started_utc": run_started_utc,
        "run_completed_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "elapsed_seconds": time.time() - run_started_epoch,
    }
    payload = {
        "schema": "steering-run/v2",
        "spec": spec,
        "method": method,
        "design": design,
        "runtime": runtime,
        "direction_integrity": direction_integrity,
        "direction_diagnostics": diagnostics,
        "records": records,
    }
    rendered_json = json.dumps(payload, indent=1, ensure_ascii=False)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=out_dir,
        prefix=out_path.name + ".", suffix=".tmp", delete=False,
    ) as handle:
        handle.write(rendered_json)
        temp_path = Path(handle.name)
    temp_path.replace(out_path)
    print(f"\nfull results (raw outputs included): {out_path}")

    # readable transcript, automatically, next to the figures pipeline
    try:
        from analysis.transcripts import render
        md_dir = (
            out_dir / "transcripts"
            if args.output_dir
            else Path(__file__).resolve().parents[1] / "analysis" / "transcripts"
        )
        md_dir.mkdir(parents=True, exist_ok=True)
        md_path = md_dir / (out_path.stem + ".md")
        md_path.write_text(
            render(payload),
            encoding="utf-8",
        )
        print(f"transcript: {md_path}")
    except Exception as e:  # a broken transcript must never kill a paid run
        print(f"(transcript generation failed: {e})")
    print("read a few raw outputs before believing the numbers.")


if __name__ == "__main__":
    main()
