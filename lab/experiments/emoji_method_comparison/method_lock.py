"""Validation for the frozen emoji CAA/NLA/J-lens method lock."""

from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping

import yaml


HERE = Path(__file__).resolve().parent
LOCK_PATH = HERE / "methods.lock.yaml"
PROMPT_BANK_PATH = HERE / "prompts.heldout.yaml"
SCHEMA = "emoji-method-comparison-lock/v5"
PROMPT_SCHEMA = "emoji-method-comparison-prompt-bank/v1"
STUDY_ID = "crossmodel-qwen-l20-gemma-l32-caa-nla-emoji-v5"
PHRASE = "The response should include Unicode emojis and emoticons."
PROMPT_CONTROL = "Use context-appropriate Unicode emojis and emoticons in your response."
GLYPH = "\U0001F60A"
PRIMARY_IDS = ("caa", "nla_phrase", "nla_glyph")
EXPLORATORY_IDS = ("jlens_word_atom",)
PROMPT_PAYLOAD_SHA256 = "db47f49590ed8532798e351a84d311e81653b3ef1cca574697857791353b98b0"
CALIBRATION_PAYLOAD_SHA256 = "9215021227360177a1d6aff82ffa84a1338473b0859c51ee1816e7f6124db48b"
CAA_PAYLOAD_SHA256 = "2adbe915db030b2c7015d4bae03c5eff19e7dd6f96a0e5147a2b2e34f2e5ac48"
FIT_CORPUS_SHA256 = "68d2faa4ecbf7b98acc69ab71bcb3b47304f3be3075a29293a13f9fb777f7444"
FORBIDDEN_PROMPT_PATTERN = re.compile(
    r"emoji|emoticon|smiley|pictograph|unicode|glyph|"
    r"\b(?:latest|breaking news|celebrity|actor|actress|singer|politician|"
    r"president)\b|who is |what is the name|what are the names|"
    r"\b(?:json|bullet points?|table format)\b",
    re.IGNORECASE,
)


class MethodLockError(ValueError):
    """Raised when the frozen protocol is incomplete or internally inconsistent."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise MethodLockError(message)


def load_method_lock(path: Path = LOCK_PATH) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    _require(isinstance(data, dict), "method lock must be a mapping")
    validate_method_lock(data)
    return data


def prompt_payload_sha256(prompts: list[Mapping[str, Any]]) -> str:
    payload = json.dumps(
        prompts, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def load_prompt_bank(path: Path = PROMPT_BANK_PATH) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    _require(isinstance(data, dict), "prompt bank must be a mapping")
    validate_prompt_bank(data)
    return data


def validate_prompt_bank(data: Mapping[str, Any]) -> None:
    _require(data.get("schema") == PROMPT_SCHEMA, "wrong prompt-bank schema")
    _require(
        data.get("study_id") == STUDY_ID,
        "prompt bank belongs to another study",
    )
    _require(
        data.get("status") == "selected_frozen_pre_output",
        "held-out prompts are not frozen",
    )

    source = data.get("source", {})
    _require(
        source.get("revision")
        == "ad8a5d60c4616b599c24dd6689f05f696ec610f3",
        "source revision drifted",
    )
    _require(
        source.get("inventory") == "prod_2b_l10_v1",
        "source inventory drifted",
    )
    _require(source.get("split") == "test", "held-out source must be test")
    _require(
        source.get("file_sha256")
        == "04924a6a3c36ccf3d91fb980dac2da9175274883d649e884c25ede0c904c58ea",
        "source file hash drifted",
    )

    selection = data.get("selection", {})
    _require(
        selection.get("expected_outputs_used") is False,
        "expected outputs cannot select prompts",
    )
    _require(
        selection.get("treatment_outputs_seen") is False,
        "prompt bank was not frozen pre-output",
    )
    _require(
        selection.get("prompt_text_editing") == "forbidden",
        "source prompt editing was enabled",
    )

    prompts = data.get("prompts")
    _require(
        isinstance(prompts, list) and len(prompts) == 40,
        "prompt bank must contain 40 rows",
    )
    _require(
        prompt_payload_sha256(prompts) == PROMPT_PAYLOAD_SHA256,
        "frozen prompt payload drifted",
    )

    expected_ids = [f"ordinary-{i:02d}" for i in range(1, 31)] + [
        f"formal-{i:02d}" for i in range(1, 11)
    ]
    _require(
        [row.get("id") for row in prompts] == expected_ids,
        "prompt IDs or order drifted",
    )
    _require(
        len({row.get("source_row") for row in prompts}) == 40,
        "source rows are duplicated",
    )
    _require(
        len({row.get("text") for row in prompts}) == 40,
        "prompt texts are duplicated",
    )

    tracks: Counter[str] = Counter()
    for row in prompts:
        text = row.get("text")
        _require(
            isinstance(text, str) and 30 <= len(text) <= 300,
            f"invalid prompt length for {row.get('id')}",
        )
        _require(
            "\n" not in text and text == " ".join(text.split()),
            f"prompt whitespace drifted for {row.get('id')}",
        )
        _require(
            hashlib.sha256(text.encode("utf-8")).hexdigest()
            == row.get("input_sha256"),
            f"prompt hash drifted for {row.get('id')}",
        )
        _require(
            not FORBIDDEN_PROMPT_PATTERN.search(text),
            f"forbidden target/name/news/format leakage in {row.get('id')}",
        )
        _require(
            "output" not in row and "output_concept" not in row,
            f"expected output leaked into {row.get('id')}",
        )
        tracks[str(row.get("track"))] += 1

    _require(
        tracks
        == Counter(
            {"ordinary_optional": 30, "formal_technical_transfer": 10}
        ),
        "prompt track counts drifted",
    )


def validate_method_lock(data: Mapping[str, Any]) -> None:
    _require(data.get("schema") == SCHEMA, "wrong method-lock schema")
    _require(data.get("study_id") == STUDY_ID, "method lock belongs to another study")
    _require(
        data.get("status")
        == "methods_and_all_input_banks_frozen_build_receipts_pending",
        "methods and input banks are not frozen",
    )
    amendment = data.get("amendment_from_v2", {})
    _require(
        amendment.get("prior_status")
        == "calibration_only_pilot_complete_no_heldout_generation_or_analysis",
        "v2 calibration-only status is not recorded",
    )
    _require(
        amendment.get("only_protocol_change")
        == "common_intervention.max_new_tokens_from_96_to_192",
        "v3 amendment scope drifted",
    )
    _require(
        amendment.get("heldout_specs_materialized_or_outputs_generated_in_v2") is False,
        "v2 held-out non-generation is not recorded",
    )
    amendment_v3 = data.get("amendment_from_v3", {})
    _require(
        amendment_v3.get("prior_status")
        == "calibration_only_complete_no_heldout_generation_or_analysis",
        "v3 calibration-only status is not recorded",
    )
    _require(
        amendment_v3.get("only_protocol_change")
        == "evaluation.quality_gate.reject_hit_max_new_tokens_from_true_to_false",
        "v4 amendment scope drifted",
    )
    _require(
        amendment_v3.get("heldout_specs_materialized_or_outputs_generated_in_v3") is False,
        "v3 held-out non-generation is not recorded",
    )
    amendment_v4 = data.get("amendment_from_v4", {})
    _require(
        amendment_v4.get("prior_status")
        == "qwen_complete_gemma_no_behavioral_output",
        "v4 completion boundary is not recorded",
    )
    _require(
        amendment_v4.get("only_protocol_change")
        == "remove_jlens_word_atom_from_gemma_behavioral_generation_and_primary_claim_family",
        "v5 amendment scope drifted",
    )
    _require(
        amendment_v4.get("qwen_v4_results_preserved_unchanged") is True,
        "completed Qwen v4 results are not explicitly preserved",
    )
    _require(
        amendment_v4.get("gemma_behavioral_outputs_generated_in_v4") is False,
        "Gemma v4 non-generation boundary is not recorded",
    )

    target = data.get("target", {})
    _require(target.get("requested_amount") == "unrestricted", "emoji amount must remain unrestricted")
    _require(target.get("phrase_surface") == PHRASE, "phrase surface drifted")
    _require(target.get("explicit_prompt_positive_control") == PROMPT_CONTROL, "prompt control drifted")
    _require(target.get("glyph_surface") == GLYPH, "glyph surface drifted")
    _require(target.get("glyph_codepoint") == "U+1F60A", "glyph codepoint drifted")
    _require(target.get("glyph_count") == 1, "glyph edit must contain exactly one glyph")

    models = data.get("model_matrix", {})
    _require(
        set(models) == {"qwen", "gemma3-12b", "invariants"},
        "two-model matrix drifted",
    )
    expected_models = {
        "qwen": {
            "space_id": "qwen2.5-7b-instruct.resid-post.L20.d3584",
            "layer": 20,
            "hidden_width": 3584,
            "calibration_alphas": [0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6],
            "checkpoint_dirs": {
                "base": "qwen2.5-7b-instruct", "av": "nla-av", "ar": "nla-ar"
            },
            "checkpoint_sources": {
                "base": {"repository": "Qwen/Qwen2.5-7B-Instruct", "revision": "a09a35458c702b33eeacc393d103063234e8bc28"},
                "av": {"repository": "kitft/nla-qwen2.5-7b-L20-av", "revision": "b88469162777ae6553bc14208eb0cb579336f8f4"},
                "ar": {"repository": "kitft/nla-qwen2.5-7b-L20-ar", "revision": "e2c9e57eac213d37a31612087f645ab6332c1bb6"},
            },
            "lens_sha256": "84564f848c839d97eb0412ea85ae6a9b0f09f83c1ff75439fdc1966805367bd8",
            "substrate_sha256": "004824f65a1e09199a95d744864afd0b0f42654b7cec87fe5cbff01a33a523b9",
        },
        "gemma3-12b": {
            "space_id": "gemma3-12b-it.resid-post.L32.d3840",
            "layer": 32,
            "hidden_width": 3840,
            "calibration_alphas": [0, 0.05, 0.1, 0.15, 0.2],
            "checkpoint_dirs": {
                "base": "gemma-3-12b-it", "av": "nla-gemma3-12b-av", "ar": "nla-gemma3-12b-ar"
            },
            "checkpoint_sources": {
                "base": {"repository": "google/gemma-3-12b-it", "revision": "96b6f1eccf38110c56df3a15bffe176da04bfd80"},
                "av": {"repository": "kitft/nla-gemma3-12b-L32-av", "revision": "7aec22599e8a9cd533564868999b443fcc963cf4"},
                "ar": {"repository": "kitft/nla-gemma3-12b-L32-ar", "revision": "3d6901d8243182d642af9dea452ca91549a94615"},
            },
            "lens_sha256": "8ef476fae7487e42e1540b87fa0e6b8c5ad4d498aebe51a3b5dbca7c2ad4df39",
            "substrate_sha256": "cc3c98abde5830378b6f15554cbf66abcea68f624c3a3a669acc1a765943c97a",
        },
    }
    for model_key, expected in expected_models.items():
        model = models[model_key]
        for field in ("space_id", "layer", "hidden_width", "calibration_alphas", "checkpoint_dirs", "checkpoint_sources"):
            _require(model.get(field) == expected[field], f"{model_key} {field} drifted")
        _require(model.get("stream_site") == "block_output_residual", f"{model_key} stream site drifted")
        substrate = model.get("nla_substrate", {})
        _require(substrate.get("prompt") == "Tell me about your day.", f"{model_key} substrate prompt drifted")
        _require(substrate.get("source_sha256") == expected["substrate_sha256"], f"{model_key} substrate hash drifted")
        _require(len(str(substrate.get("verbalization_sha256", ""))) == 64, f"{model_key} verbalization hash missing")
        lens = model.get("jlens", {})
        _require(lens.get("sha256") == expected["lens_sha256"], f"{model_key} lens hash drifted")
        _require(len(lens.get("split_half_lenses", [])) == 2, f"{model_key} split lenses drifted")
    invariants = models["invariants"]
    _require(invariants.get("vector_transport_between_models") == "forbidden", "cross-model vector transport enabled")
    _require(invariants.get("alpha_selection") == "independent_per_model_and_treatment", "cross-model alpha selection drifted")

    construction = data.get("construction", {})
    _require(set(construction) == {"caa", "nla_phrase", "nla_glyph", "jlens_word_atom"}, "construction set drifted")

    caa = construction["caa"]
    _require(caa.get("method_root") == "caa_mean_activation_contrast", "CAA root mislabeled")
    _require(caa.get("pair_count") == 300, "CAA pair count drifted")
    _require(caa.get("construction_manifest") == "experiments/emoji_method_comparison/caa.construction.yaml", "CAA manifest drifted")
    _require(caa.get("construction_manifest_status") == "selected_frozen_pre_output", "CAA rows are not frozen")
    _require(caa.get("construction_payload_sha256") == CAA_PAYLOAD_SHA256, "CAA payload lock drifted")
    _require(caa.get("option_balance") == "exactly 150 pairs with positive=A and 150 with positive=B", "CAA answer letters are not balanced")
    _require(caa.get("positive_requirement") == PHRASE, "CAA positive surface drifted")
    _require("mean_i(" in caa.get("formula", ""), "CAA formula is not a mean difference")

    for arm_id, expected_text in (("nla_phrase", PHRASE), ("nla_glyph", GLYPH)):
        arm = construction[arm_id]
        _require(arm.get("method_root") == "nla_av_edit_ar", f"{arm_id} is not AV-edit-AR")
        _require(arm.get("averaging") == "none", f"{arm_id} must not average")
        _require(arm.get("append_separator") == "\n\n", f"{arm_id} separator drifted")
        _require(arm.get("appended_text") == expected_text, f"{arm_id} edit drifted")
        _require("AR(z_original" in arm.get("formula", ""), f"{arm_id} formula drifted")

    jlens = construction["jlens_word_atom"]
    _require(jlens.get("method_root") == "j_lens_atom", "J-lens root mislabeled")
    _require(jlens.get("token_surface") == " emoji", "J-lens token surface drifted")
    _require(jlens.get("token_id") == "pending_per_model_pod_tokenizer_receipt", "J-lens token receipt ceased to be model-specific")
    _require(jlens.get("token_gate") == "exact_single_token_encode_decode_round_trip_or_arm_fails", "J-lens token gate weakened")
    _require(jlens.get("minimum_split_half_atom_cosine") == 0.90, "J-lens split-half gate drifted")
    _require(jlens.get("fit_corpus_sha256") == FIT_CORPUS_SHA256, "J-lens fit-corpus hash drifted")
    _require(
        jlens.get("fit_corpus_hash_rule")
        == "utf8_bytes_with_newlines_normalized_to_lf",
        "J-lens fit-corpus hash rule drifted",
    )
    _require(jlens.get("glyph_diagnostic", {}).get("behavioral_arm") is False, "unregistered glyph combination became behavioral")

    intervention = data.get("common_intervention", {})
    _require(intervention.get("mode") == "decode", "common intervention must remain decode-only")
    _require(intervention.get("hook_layer") == "per_model_registered_layer", "hook layer drifted")
    _require(intervention.get("vector_view") == "unit_l2", "vector normalization drifted")
    _require(intervention.get("max_new_tokens") == 192, "v4 response ceiling drifted")
    _require(intervention.get("calibration_alphas") == "model_matrix[active_model].calibration_alphas", "model-specific alpha ladders drifted")
    _require(intervention.get("heldout_prompts") == 40, "held-out prompt count drifted")
    _require(intervention.get("heldout_prompt_tracks") == {"ordinary_optional": 30, "formal_technical_transfer": 10}, "held-out track counts drifted")
    _require(intervention.get("heldout_prompt_manifest") == "experiments/emoji_method_comparison/prompts.heldout.yaml", "held-out prompt manifest drifted")
    _require(intervention.get("heldout_prompt_status") == "selected_frozen_pre_output", "held-out prompt status drifted")
    _require(intervention.get("heldout_prompt_payload_sha256") == PROMPT_PAYLOAD_SHA256, "held-out prompt lock hash drifted")
    _require(intervention.get("calibration_prompts") == 6, "calibration prompt count drifted")
    _require(intervention.get("calibration_prompt_source") == "historical_neutral_chat_bank", "calibration source drifted")
    _require(intervention.get("calibration_prompt_manifest") == "experiments/emoji_method_comparison/prompts.calibration.yaml", "calibration manifest drifted")
    _require(intervention.get("calibration_prompt_status") == "selected_frozen_pre_output", "calibration prompts are not frozen")
    _require(intervention.get("calibration_prompt_payload_sha256") == CALIBRATION_PAYLOAD_SHA256, "calibration payload lock drifted")
    _require(intervention.get("calibration_seed_offset") == 10000, "calibration seed offset drifted")
    _require(intervention.get("heldout_seed_offset") == 20000, "held-out seed offset drifted")
    _require(intervention.get("heldout_chunk_size_prompts") == 5, "held-out chunk size drifted")
    _require(intervention.get("chunk_resume_rule") == "exact_spec_integrity_and_expected_record_count", "resume rule drifted")
    _require(intervention.get("canary", {}).get("excluded_from_research_corpus") is True, "canary entered the research corpus")

    selection = data.get("calibration_selection", {})
    _require(selection.get("schema") == "emoji-method-comparison-alpha-selection/v1", "alpha-selection schema drifted")
    _require(selection.get("primary_metric") == "target_present", "alpha-selection endpoint drifted")
    _require(selection.get("safety_passing_fraction") == 0.90, "alpha safety threshold drifted")
    _require(selection.get("no_heldout_data_access") is True, "held-out data may not select alpha")
    _require(selection.get("no_minimum_observed_effect_for_selection") is True, "alpha selection started filtering null effects")
    _require(
        selection.get("failed_arm_policy")
        == "record_no_safe_alpha_and_exclude_from_heldout",
        "failed-arm exclusion policy drifted",
    )

    primary = data.get("arms", {}).get("primary_treatments", [])
    primary_ids = tuple(row.get("id") for row in primary)
    _require(primary_ids == PRIMARY_IDS, "primary treatment family drifted")
    exploratory = data.get("arms", {}).get("exploratory_preserved", [])
    exploratory_ids = tuple(row.get("id") for row in exploratory)
    _require(exploratory_ids == EXPLORATORY_IDS, "exploratory arm registry drifted")
    _require(
        exploratory[0].get("behavioral_generation") is False,
        "J-lens behavioral generation was re-enabled",
    )
    evaluation = data.get("evaluation", {})
    _require(evaluation.get("model_results_are_stratified") is True, "model results may not be pooled")
    _require(evaluation.get("cross_model_pooling") == "forbidden", "cross-model pooling enabled")
    _require(evaluation.get("primary_track") == "ordinary_optional", "primary prompt track drifted")
    _require(
        evaluation.get("primary_claim_multiplicity_family") == list(PRIMARY_IDS),
        "primary multiplicity family drifted",
    )
    _require(
        evaluation.get("multiplicity")
        == "Holm_across_up_to_three_nonexcluded_primary_treatments_within_each_model",
        "multiplicity rule drifted",
    )
    _require(evaluation.get("prompt_block_bootstrap") == {"draws": 10000, "seed": 20260824}, "bootstrap lock drifted")
    _require(evaluation.get("prompt_block_signflip", {}).get("monte_carlo_draws_above_exact_limit") == 200000, "sign-flip lock drifted")
    _require(
        evaluation.get("quality_gate", {}).get("reject_hit_max_new_tokens") is False,
        "v4 truncation reporting policy drifted",
    )
    quality = data.get("instruction_quality", {})
    _require(quality.get("status") == "post_generation_blinded_rating_required", "instruction-quality status drifted")
    _require(quality.get("raters_required") == 1, "siloed rating count drifted")
    _require(quality.get("arm_identity_hidden_in_sheet") is True, "quality sheet is not blinded")


if __name__ == "__main__":
    lock = load_method_lock()
    prompts = load_prompt_bank()
    from experiments.emoji_method_comparison.freeze_data import (
        CAA_PATH, CALIBRATION_PATH, load_yaml,
        validate_caa_manifest, validate_calibration_bank,
    )
    calibration = load_yaml(CALIBRATION_PATH)
    caa = load_yaml(CAA_PATH)
    validate_calibration_bank(calibration, heldout=prompts)
    validate_caa_manifest(caa, calibration=calibration, heldout=prompts)
    print(
        f"valid {lock['schema']} ({lock['study_id']}), "
        f"calibration={len(calibration['prompts'])}, "
        f"heldout={len(prompts['prompts'])}, CAA={len(caa['rows'])}"
    )
