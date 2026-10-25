"""Controlled taxonomy for steering-vector ancestry and administration.

Historical reports used "NLA steering" for several different constructions.
The normalized record here deliberately separates:

* root construction -- how the original vector was made;
* derivation -- any transformation of a named parent vector;
* vector view -- native versus normalized representation;
* intervention -- how the vector was administered;
* study design -- evidential intent/role, kept outside the method record.

New specs should store an explicit ``method`` block. Legacy specs remain
readable through conservative rules tied to audited builder provenance. Every
legacy classification is labelled; unknown file keys fail closed as unknown.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any


TAXONOMY_ID = "steering-taxonomy/v1"
DESIGN_SCHEMA_ID = "steering-study/v1"

ROOT_METHODS = {
    "none",
    "prompt_instruction",
    "diffmean_activation_contrast",
    "nla_ar_contrast",
    "nla_av_edit_ar",
    "caa_mean_activation_contrast",
    "j_lens_atom",
    "random_isotropic",
    "unknown",
}

TEXT_ORIGINS = {
    "none",
    "authored",
    "template_instantiated",
    "dataset",
    "av_generated_substrate",
    "not_applicable",
    "unknown",
}

CONTRAST_RELATIONS = {
    "none",
    "paired_descriptions",
    "edited_vs_original",
    "matched_activation_pairs",
    "not_applicable",
    "unknown",
}

EDIT_GRANULARITIES = {
    "none",
    "single_site",
    "phrase",
    "clause",
    "document",
    "not_applicable",
    "unknown",
}

PAIRING_STYLES = {
    "matched_scaffold",
    "unconstrained",
    "not_applicable",
    "unknown",
}

DERIVATION_OPERATORS = {
    "none",
    "jspace_sparse_nonnegative",
    "j_readout_ols_split",
    "linear_combination",
    "unknown",
}

OUTPUT_ROLES = {"none", "whole", "component", "remainder", "mixture", "unknown"}
VECTOR_VIEWS = {"none", "unit_l2", "native", "unknown"}

DESIGN_INTENTS = {"screening", "exploratory", "confirmatory", "unknown"}
ARM_ROLES = {"baseline_reference", "negative_control", "treatment"}

_DERIVED_OPERATORS = {
    "jspace_sparse_nonnegative",
    "j_readout_ols_split",
    "linear_combination",
}


class MethodMetadataError(ValueError):
    """Raised when explicit taxonomy metadata is missing or inconsistent."""


def _file_ref(spec: dict[str, Any]) -> tuple[str | None, str | None]:
    ref = spec.get("direction_file")
    if not ref:
        return None, None
    normalized = str(ref).replace("\\", "/")
    if "#" not in normalized:
        return normalized, None
    return tuple(normalized.rsplit("#", 1))  # type: ignore[return-value]


def _token_scope(mode: str) -> str:
    return {
        "all": "prompt_and_decode_all",
        "decode": "decode_only",
        "position": "single_prompt_position",
        "last_and_decode": "last_prompt_position_and_each_decode_token",
    }.get(mode, "unknown")


def _intervention(spec: dict[str, Any], root_method: str) -> dict[str, Any]:
    if root_method in {"none", "prompt_instruction"}:
        return {
            "kind": "none",
            "token_scope": "none",
            "position": None,
            "scaling_rule": "none",
            "formula": None,
        }
    mode = str(spec.get("mode", "all"))
    return {
        "kind": "additive_residual",
        "token_scope": _token_scope(mode),
        "position": int(spec.get("position", -1)) if mode == "position" else None,
        "scaling_rule": "relative_token_l2",
        "formula": "h_prime = h + alpha * l2(h) * unit_l2(d)",
    }


def _root(
    method: str,
    *,
    text_origin: str,
    contrast_relation: str,
    edit_granularity: str,
    pairing_style: str,
) -> dict[str, str]:
    return {
        "method": method,
        "text_origin": text_origin,
        "contrast_relation": contrast_relation,
        "edit_granularity": edit_granularity,
        "pairing_style": pairing_style,
    }


def _normalized(
    spec: dict[str, Any],
    *,
    root: dict[str, str],
    derivation_operator: str = "none",
    output_role: str = "whole",
    parent_vector_key: str | None = None,
    derivation_parameters: dict[str, Any] | None = None,
    vector_view: str = "unit_l2",
    origin: str,
    confidence: str = "high",
    notes: list[str] | None = None,
) -> dict[str, Any]:
    path, key = _file_ref(spec)
    return {
        "schema": TAXONOMY_ID,
        "root": deepcopy(root),
        "derivation": {
            "operator": derivation_operator,
            "output_role": output_role,
            "parent_vector_key": parent_vector_key,
            "parameters": deepcopy(derivation_parameters),
        },
        "vector_view": vector_view,
        "source_artifact": {"path": path, "key": key},
        "intervention": _intervention(spec, root["method"]),
        "classification": {
            "origin": origin,
            "confidence": confidence,
            "notes": list(notes or []),
        },
    }


def _none_root() -> dict[str, str]:
    return _root(
        "none", text_origin="none", contrast_relation="none",
        edit_granularity="none", pairing_style="not_applicable",
    )


def _random_root() -> dict[str, str]:
    return _root(
        "random_isotropic", text_origin="not_applicable",
        contrast_relation="not_applicable", edit_granularity="not_applicable",
        pairing_style="not_applicable",
    )


def _ar_root(pairing_style: str = "unknown") -> dict[str, str]:
    return _root(
        "nla_ar_contrast", text_origin="authored",
        contrast_relation="paired_descriptions", edit_granularity="not_applicable",
        pairing_style=pairing_style,
    )


def _av_edit_root(granularity: str) -> dict[str, str]:
    return _root(
        "nla_av_edit_ar", text_origin="av_generated_substrate",
        contrast_relation="edited_vs_original", edit_granularity=granularity,
        pairing_style="not_applicable",
    )


def _emoji_parent(key: str) -> dict[str, str] | None:
    if key == "freeform":
        return _ar_root()
    if key.startswith("phrase_"):
        return _av_edit_root("phrase")
    if key.startswith("single_") or key.startswith("s2_"):
        return _av_edit_root("single_site")
    return None


def _gemma_parent(key: str) -> dict[str, str] | None:
    if key in {"g_freeform", "g_dwell", "g_lion", "g_len_seven"}:
        return _ar_root()
    if key in {"g_emoji", "g_emojis", "g_emoticon", "g_smiley", "g_glyphword"}:
        return _av_edit_root("single_site")
    if key.startswith("g_phrase_"):
        return _av_edit_root("phrase")
    return None


def _greedy_k25() -> dict[str, Any]:
    return {
        "method": "reference_inspired_greedy_nnls",
        "dictionary": "full_vocabulary_j_lens_atoms",
        "candidate_pool": "full_vocabulary",
        "atom_normalization": "unit_l2",
        "solver": "greedy_max_positive_correlation_with_nnls_refit",
        "paper_solver_relation": "reference_inspired_divergence",
        "nonnegative": True,
        "k_requested": 25,
    }


def _legacy_note() -> list[str]:
    return [
        "Backfilled from audited executable semantics and vector-builder lineage; "
        "the immutable raw result predates explicit taxonomy metadata."
    ]


def _whole(spec: dict[str, Any], root: dict[str, str]) -> dict[str, Any]:
    return _normalized(
        spec, root=root, origin="legacy_reviewed_backfill", notes=_legacy_note()
    )


def _derived(
    spec: dict[str, Any],
    root: dict[str, str],
    *,
    operator: str,
    role: str,
    parent_key: str,
    parameters: dict[str, Any],
    extra_notes: list[str] | None = None,
) -> dict[str, Any]:
    return _normalized(
        spec,
        root=root,
        derivation_operator=operator,
        output_role=role,
        parent_vector_key=parent_key,
        derivation_parameters=parameters,
        origin="legacy_reviewed_backfill",
        notes=_legacy_note() + list(extra_notes or []),
    )


def _infer_file_method(spec: dict[str, Any], path: str, key: str) -> dict[str, Any]:
    if path.endswith("emoji_methods/data/vectors.npz"):
        if key == "freeform":
            return _whole(spec, _ar_root())
        if key.startswith("single_"):
            return _whole(spec, _av_edit_root("single_site"))
        if key.startswith("phrase_"):
            return _whole(spec, _av_edit_root("phrase"))
        if key in {"readable", "leftover"}:
            return _derived(
                spec,
                _ar_root(),
                operator="j_readout_ols_split",
                role="component" if key == "readable" else "remainder",
                parent_key="freeform",
                parameters={
                    "method": "signed_ordinary_least_squares",
                    "dictionary": "parent_top_50_j_lens_readout_atoms",
                    "k_requested": 50,
                    "nonnegative": False,
                },
                extra_notes=["Legacy v1 proxy; not a sparse nonnegative J-space decomposition."],
            )

    if path.endswith("emoji_methods/data/vectors_v2.npz") and key.startswith("s2_"):
        return _whole(spec, _av_edit_root("single_site"))

    if path.endswith("emoji_methods/data/decomposition_v2.npz"):
        if key.startswith("comp25_") or key.startswith("resid25_"):
            parent_key = key.split("_", 1)[1]
            parent = _emoji_parent(parent_key)
            if parent is not None:
                return _derived(
                    spec,
                    parent,
                    operator="jspace_sparse_nonnegative",
                    role="component" if key.startswith("comp25_") else "remainder",
                    parent_key=parent_key,
                    parameters=_greedy_k25(),
                )

    if path.endswith("wordle2/data/wordle_vectors.npz"):
        if key.startswith("comp25_") or key.startswith("resid25_"):
            parent_key = key.split("_", 1)[1]
            return _derived(
                spec,
                _ar_root(),
                operator="jspace_sparse_nonnegative",
                role="component" if key.startswith("comp25_") else "remainder",
                parent_key=parent_key,
                parameters=_greedy_k25(),
            )
        if key.startswith("vephrase_"):
            return _whole(spec, _av_edit_root("phrase"))
        if key.startswith("ve1_"):
            return _whole(spec, _av_edit_root("single_site"))
        # All swept Wordle2 whole vectors came from independently authored AR pairs.
        if (key.startswith("ff_") or key.startswith("len_")
                or key.startswith("poor_") or key.startswith("anchor_")):
            return _whole(spec, _ar_root())

    if path.endswith("gemma_core/data/gemma_vectors.npz"):
        if key.startswith("comp25_") or key.startswith("resid25_"):
            parent_key = key.split("_", 1)[1]
            parent = _gemma_parent(parent_key)
            if parent is not None:
                return _derived(
                    spec,
                    parent,
                    operator="jspace_sparse_nonnegative",
                    role="component" if key.startswith("comp25_") else "remainder",
                    parent_key=parent_key,
                    parameters=_greedy_k25(),
                )
        parent = _gemma_parent(key)
        if parent is not None:
            return _whole(spec, parent)

    return _normalized(
        spec,
        root=_root(
            "unknown", text_origin="unknown", contrast_relation="unknown",
            edit_granularity="unknown", pairing_style="unknown",
        ),
        derivation_operator="unknown",
        output_role="unknown",
        vector_view="unknown",
        origin="legacy_unresolved",
        confidence="low",
        notes=_legacy_note() + [f"No reviewed rule matches {path}#{key}."],
    )


def infer_legacy_method(spec: dict[str, Any]) -> dict[str, Any]:
    """Backfill pre-taxonomy specs while preserving that provenance was absent."""
    direction = spec.get("direction")
    path, key = _file_ref(spec)
    if not direction and not path:
        return _normalized(
            spec, root=_none_root(), output_role="none", vector_view="none",
            origin="legacy_reviewed_backfill", notes=_legacy_note(),
        )
    if direction == "random":
        return _whole(spec, _random_root())
    if isinstance(direction, dict):
        # The generic runner sends both researcher-supplied descriptions directly
        # through AR. No AV-derived substrate is created by this code path.
        return _whole(spec, _ar_root())
    if path and key:
        return _infer_file_method(spec, path, key)
    return _normalized(
        spec,
        root=_root(
            "unknown", text_origin="unknown", contrast_relation="unknown",
            edit_granularity="unknown", pairing_style="unknown",
        ),
        derivation_operator="unknown", output_role="unknown", vector_view="unknown",
        origin="legacy_unresolved", confidence="low",
        notes=_legacy_note() + ["Direction representation is not recognized."],
    )


def _validate_root(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise MethodMetadataError("method.root must be a mapping")
    required = {
        "method", "text_origin", "contrast_relation", "edit_granularity",
        "pairing_style",
    }
    missing = required - set(raw)
    extra = set(raw) - required
    if missing or extra:
        raise MethodMetadataError(
            f"method.root missing={sorted(missing)} unknown={sorted(extra)}"
        )
    root = {key: str(raw[key]) for key in required}
    checks = (
        ("method", ROOT_METHODS - {"unknown"}),
        ("text_origin", TEXT_ORIGINS - {"unknown"}),
        ("contrast_relation", CONTRAST_RELATIONS - {"unknown"}),
        ("edit_granularity", EDIT_GRANULARITIES - {"unknown"}),
        ("pairing_style", PAIRING_STYLES - {"unknown"}),
    )
    for field, allowed in checks:
        if root[field] not in allowed:
            raise MethodMetadataError(f"invalid explicit root.{field}={root[field]!r}")
    return root


def _validate_derivation(raw: Any) -> tuple[str, str, str | None, dict[str, Any] | None]:
    if not isinstance(raw, dict):
        raise MethodMetadataError("method.derivation must be a mapping")
    allowed = {"operator", "output_role", "parent_vector_key", "parameters"}
    extra = set(raw) - allowed
    if extra:
        raise MethodMetadataError(f"unknown method.derivation fields: {sorted(extra)}")
    operator = str(raw.get("operator", "none"))
    role = str(raw.get("output_role", "whole"))
    parent = raw.get("parent_vector_key")
    parameters = deepcopy(raw.get("parameters"))
    if operator not in DERIVATION_OPERATORS - {"unknown"}:
        raise MethodMetadataError(f"invalid derivation operator {operator!r}")
    if role not in OUTPUT_ROLES - {"unknown"}:
        raise MethodMetadataError(f"invalid derivation output_role {role!r}")
    if operator in _DERIVED_OPERATORS:
        if not parent:
            raise MethodMetadataError(f"derived operator {operator!r} needs parent_vector_key")
        if not isinstance(parameters, dict):
            raise MethodMetadataError(f"derived operator {operator!r} needs parameters")
    else:
        if parent is not None or parameters is not None or role not in {"whole", "none"}:
            raise MethodMetadataError("non-derived vectors cannot declare parent/parameters")
    return operator, role, str(parent) if parent is not None else None, parameters


def _validate_explicit(spec: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
    allowed = {"schema", "root", "derivation", "vector_view", "notes"}
    extra = set(raw) - allowed
    if extra:
        raise MethodMetadataError(f"unknown method fields: {sorted(extra)}")
    if raw.get("schema", TAXONOMY_ID) != TAXONOMY_ID:
        raise MethodMetadataError(f"method.schema must be {TAXONOMY_ID!r}")
    root = _validate_root(raw.get("root"))
    operator, role, parent, parameters = _validate_derivation(raw.get("derivation", {}))
    view = str(raw.get("vector_view", "unit_l2"))
    if view not in VECTOR_VIEWS - {"unknown"}:
        raise MethodMetadataError(f"invalid explicit vector_view {view!r}")
    if root["method"] in {"none", "prompt_instruction"}:
        if role != "none" or view != "none":
            raise MethodMetadataError(
                "non-residual baseline must use output_role/vector_view 'none'"
            )
    elif role == "none" or view == "none":
        raise MethodMetadataError("steered method cannot use output_role/vector_view 'none'")

    direction = spec.get("direction")
    vector_path, vector_key = _file_ref(spec)
    if not direction and not vector_path:
        expected_root = {"none", "prompt_instruction"}
    elif direction == "random":
        expected_root = "random_isotropic"
    elif isinstance(direction, dict):
        expected_root = "nla_ar_contrast"
    else:
        expected_root = None  # file vectors can represent any non-random root
    if expected_root is not None and (
        root["method"] not in expected_root
        if isinstance(expected_root, set) else root["method"] != expected_root
    ):
        raise MethodMetadataError(
            f"spec direction encoding requires root.method {expected_root!r}, "
            f"got {root['method']!r}"
        )
    if vector_path is None and operator != "none":
        raise MethodMetadataError("derived vectors must be referenced by direction_file")
    if vector_path is not None and vector_key is None:
        raise MethodMetadataError("direction_file must include an explicit #key")
    notes = raw.get("notes", [])
    if isinstance(notes, str):
        notes = [notes]
    if not isinstance(notes, list) or not all(isinstance(item, str) for item in notes):
        raise MethodMetadataError("method.notes must be a string or list of strings")
    return _normalized(
        spec, root=root, derivation_operator=operator, output_role=role,
        parent_vector_key=parent, derivation_parameters=parameters,
        vector_view=view, origin="explicit_spec", notes=notes,
    )


def resolve_method(spec: dict[str, Any]) -> dict[str, Any]:
    """Return a normalized taxonomy record for an explicit or legacy spec."""
    raw = spec.get("method")
    if raw is None:
        return infer_legacy_method(spec)
    if not isinstance(raw, dict):
        raise MethodMetadataError("method must be a mapping")
    return _validate_explicit(spec, raw)


def resolve_design(spec: dict[str, Any], method: dict[str, Any]) -> dict[str, Any]:
    """Normalize evidence intent separately from vector ancestry."""
    root_method = method["root"]["method"]
    expected_role = (
        "baseline_reference" if root_method in {"none", "prompt_instruction"} else
        "negative_control" if root_method == "random_isotropic" else
        "treatment"
    )
    raw = spec.get("design")
    if raw is None:
        return {
            "schema": DESIGN_SCHEMA_ID,
            "intent": "unknown",
            "preregistered": "unknown",
            "arm_role": expected_role,
            "classification_origin": "legacy_unspecified",
            "notes": [],
        }
    if not isinstance(raw, dict):
        raise MethodMetadataError("design must be a mapping")
    allowed = {"schema", "intent", "preregistered", "arm_role", "notes"}
    extra = set(raw) - allowed
    if extra:
        raise MethodMetadataError(f"unknown design fields: {sorted(extra)}")
    if raw.get("schema", DESIGN_SCHEMA_ID) != DESIGN_SCHEMA_ID:
        raise MethodMetadataError(f"design.schema must be {DESIGN_SCHEMA_ID!r}")
    intent = str(raw.get("intent", "unknown"))
    if intent not in DESIGN_INTENTS:
        raise MethodMetadataError(f"invalid design intent {intent!r}")
    if intent == "confirmatory" and method["classification"]["origin"] != "explicit_spec":
        raise MethodMetadataError(
            "confirmatory specs require an explicit method block; legacy inference is not provenance"
        )
    preregistered = raw.get("preregistered", "unknown")
    if preregistered not in {True, False, "unknown"}:
        raise MethodMetadataError("design.preregistered must be true, false, or 'unknown'")
    role = str(raw.get("arm_role", expected_role))
    if role not in ARM_ROLES or role != expected_role:
        raise MethodMetadataError(
            f"design arm_role {role!r} conflicts with method role {expected_role!r}"
        )
    notes = raw.get("notes", [])
    if isinstance(notes, str):
        notes = [notes]
    if not isinstance(notes, list) or not all(isinstance(item, str) for item in notes):
        raise MethodMetadataError("design.notes must be a string or list of strings")
    return {
        "schema": DESIGN_SCHEMA_ID,
        "intent": intent,
        "preregistered": preregistered,
        "arm_role": role,
        "classification_origin": "explicit_spec",
        "notes": notes,
    }


def short_label(method: dict[str, Any]) -> str:
    """Compact ancestry-preserving label for logs and tables."""
    root = method["root"]["method"]
    derivation = method["derivation"]
    operator = derivation["operator"]
    role = derivation["output_role"]
    if root == "none":
        return "unsteered census"
    if operator == "none":
        return f"{root} / whole"
    if operator == "jspace_sparse_nonnegative":
        k = (derivation.get("parameters") or {}).get("k_requested", "?")
        return f"{root} / greedy-NNLS k{k} {role}"
    return f"{root} / {operator} {role}"
