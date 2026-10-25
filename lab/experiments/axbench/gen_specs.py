"""Validate or materialize AxBench-derived runner specs.

Examples (CPU-only; no model is loaded):

    python experiments/axbench/gen_specs.py validate
    python experiments/axbench/gen_specs.py preview --model qwen

``preview`` writes screening-only calibration specs from the draft prompt bank.
Held-out specs are intentionally unavailable until the prompt bank and study
are frozen and an alpha-selection receipt exists.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import tempfile

import yaml

from harness import HarnessError, build_spec, canonical_sha256, load_study
from nla_lab.study import seal_spec


def build_canary_spec(bundle: dict, model_key: str, concept_key: str) -> dict:
    """Return a two-generation NLA plumbing canary from calibration prompt 1."""

    spec = build_spec(
        bundle,
        model_key=model_key,
        concept_key=concept_key,
        arm_id="nla_ar",
        phase="calibration",
        allow_draft=True,
    )
    spec.pop("spec_integrity", None)
    nonzero = [float(alpha) for alpha in spec["alphas"] if float(alpha) != 0.0]
    if not nonzero:
        raise HarnessError("canary source spec has no nonzero alpha")
    spec["name"] += "-canary"
    spec["study"]["canary"] = True
    spec["prompts"] = [spec["prompts"][0]]
    spec["alphas"] = [0.0, min(nonzero, key=abs)]
    spec["samples_per_prompt"] = 1
    spec["max_new_tokens"] = 64
    spec["expected_records"] = 2
    spec["execution_guard"].update({
        "require_clean_git": True,
        "require_external_output_dir": True,
    })
    return seal_spec(spec)


def _write_specs(specs: list[dict], out_dir: Path, manifest: dict) -> None:
    """Write only after every spec has passed all in-memory validation."""
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = dict(manifest)
    manifest["specs"] = [
        {"name": spec["name"], "integrity": spec["spec_integrity"]["sha256"]}
        for spec in specs
    ]
    manifest["manifest_sha256"] = canonical_sha256(manifest)
    manifest_slug = "-".join(
        str(manifest[key]).replace("_", "-")
        for key in ("model_key", "concept_key", "phase")
    )
    rendered = [
        (
            out_dir / f"{spec['name']}.yaml",
            yaml.safe_dump(spec, sort_keys=False, allow_unicode=True),
        )
        for spec in specs
    ]
    rendered.append((
        out_dir / f"_materialization-{manifest_slug}.yaml",
        yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True),
    ))
    for path, text in rendered:
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(text, encoding="utf-8")
        temp.replace(path)
    print(f"wrote {len(specs)} specs plus manifest to {out_dir}")


def validate_atom_report(
    bundle: dict, model_key: str, concept_key: str, report: dict
) -> dict:
    expected_space = bundle["study"]["models"][model_key]["space_id"]
    if (
        report.get("schema") != "j-lens-atom-build/v2"
        or report.get("model_key") != model_key
        or report.get("space_id") != expected_space
        or report.get("concept_registry_sha256") != bundle["hashes"]["concepts"]
    ):
        raise HarnessError("atom report model/space/concept lock mismatch")
    receipt = report.get("receipts", {}).get(concept_key)
    if not isinstance(receipt, dict):
        raise HarnessError("atom report has no receipt for the requested concept")
    if (
        receipt.get("model_key") != model_key
        or receipt.get("concept_key") != concept_key
        or receipt.get("space_id") != expected_space
        or receipt.get("concept_registry_sha256") != bundle["hashes"]["concepts"]
    ):
        raise HarnessError("atom exclusion/build receipt identity mismatch")
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("validate", "preview", "canary", "materialize")
    )
    parser.add_argument("--model", choices=("qwen", "gemma3-12b"), default="qwen")
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--concept", default=None)
    parser.add_argument("--phase", choices=("calibration", "evaluation", "transfer"), default="calibration")
    parser.add_argument("--alpha-receipt", type=Path, default=None)
    parser.add_argument("--atom-report", type=Path, default=None)
    args = parser.parse_args()

    bundle = load_study()
    report = bundle["prompt_report"]
    print(f"study: {bundle['study']['study_id']} ({bundle['study']['status']})")
    print(f"prompts: {report['prompt_count']} ({report['status']})")
    print(f"prompt hash: {report['prompt_bank_sha256']}")
    for concept, counts in report["counts"].items():
        print(f"  {concept}: {counts}")
    if args.command == "validate":
        return

    if args.command == "canary":
        concept_key = args.concept or "meanwhile"
        out_dir = args.output_dir or Path(tempfile.mkdtemp(prefix="nla-axb-canary-"))
        spec = build_canary_spec(bundle, args.model, concept_key)
        _write_specs([spec], out_dir, {
            "schema": "axbench-materialization/v1",
            "model_key": args.model,
            "concept_key": concept_key,
            "phase": "calibration_canary",
            "locks": bundle["hashes"],
            "exclusions": [],
        })
        return

    if args.command == "materialize":
        import json

        if args.concept is None:
            raise HarnessError("materialize requires --concept")
        alpha_receipt = (
            json.loads(args.alpha_receipt.read_text(encoding="utf-8"))
            if args.alpha_receipt else None
        )
        atom_report = (
            json.loads(args.atom_report.read_text(encoding="utf-8"))
            if args.atom_report else None
        )
        out_dir = args.output_dir or (
            Path(__file__).resolve().parent / "specs" / args.phase
            / args.model / args.concept
        )
        specs = []
        exclusions = []
        arms = [
            arm for arm in bundle["study"]["arms"]
            if args.phase != "calibration"
            or arm["kind"] in {"nla_ar_contrast", "j_lens_atom"}
        ]
        for arm in arms:
            if arm["kind"] == "j_lens_atom":
                concept = bundle["concepts"][args.concept]
                if "direct_j_atom" not in concept:
                    continue
                if atom_report is None:
                    raise HarnessError("eligible pure J-atom arm requires --atom-report")
                atom_receipt = validate_atom_report(
                    bundle, args.model, args.concept, atom_report
                )
                if atom_receipt.get("status") == "ineligible_tokenization":
                    if args.phase in {"evaluation", "transfer"}:
                        if not isinstance(alpha_receipt, dict) or (
                            atom_receipt.get("checkpoint_marker_sha256")
                            != alpha_receipt.get("base_checkpoint_marker_sha256")
                        ):
                            raise HarnessError(
                                "held-out atom exclusion was measured on a different tokenizer/checkpoint"
                            )
                        locked_exclusion = alpha_receipt.get(
                            "treatment_exclusions", {}
                        ).get("direct_j_atom")
                        if not isinstance(locked_exclusion, dict) or (
                            locked_exclusion.get("receipt_sha256")
                            != canonical_sha256(atom_receipt)
                        ):
                            raise HarnessError(
                                "held-out atom exclusion is not sealed into the alpha receipt"
                            )
                    print(
                        f"skipping direct J atom for {args.model}/{args.concept}: "
                        f"{atom_receipt.get('reason', 'not one token')}"
                    )
                    exclusions.append({
                        "arm_id": arm["id"],
                        "reason": "ineligible_tokenization",
                        "receipt": atom_receipt,
                        "receipt_sha256": canonical_sha256(atom_receipt),
                    })
                    continue
            else:
                atom_receipt = None
            specs.append(build_spec(
                bundle, model_key=args.model, concept_key=args.concept,
                arm_id=arm["id"], phase=args.phase,
                atom_receipt=atom_receipt, alpha_receipt=alpha_receipt,
            ))
        _write_specs(specs, out_dir, {
            "schema": "axbench-materialization/v1",
            "model_key": args.model,
            "concept_key": args.concept,
            "phase": args.phase,
            "locks": bundle["hashes"],
            "exclusions": exclusions,
        })
        return

    out_dir = args.output_dir or Path(tempfile.mkdtemp(prefix="nla-axb-preview-"))
    specs = []
    for concept in bundle["study"]["pilot_concepts"]:
        for arm in bundle["study"]["arms"]:
            if arm["kind"] not in {"nla_ar_contrast", "j_lens_atom"}:
                continue
            if arm["kind"] == "j_lens_atom":
                continue  # requires a pod-built, hashed atom receipt
            specs.append(build_spec(
                bundle, model_key=args.model, concept_key=concept,
                arm_id=arm["id"], phase="calibration", allow_draft=True,
            ))
    _write_specs(specs, out_dir, {
        "schema": "axbench-materialization/v1",
        "model_key": args.model,
        "concept_key": "all_pilot_concepts",
        "phase": "calibration_preview",
        "locks": bundle["hashes"],
        "exclusions": [{"arm_id": "direct_j_atom", "reason": "preview_has_no_atom_receipt"}],
    })


if __name__ == "__main__":
    try:
        main()
    except HarnessError as exc:
        raise SystemExit(f"preflight failed: {exc}") from exc
