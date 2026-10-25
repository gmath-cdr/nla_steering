"""Controlled casino AV-edit type x finite decode-window diagnostic."""

from __future__ import annotations

import argparse
import json
import math
import random
import re
from pathlib import Path
from typing import Any, Callable

import torch

from experiments import data, steering, vectors
from nla_inference import NLAClient, NLACritic


PROTOCOL_VERSION = "casino-av-edit-window-v1"
CASINO_TERMS = ["casino", "casinos", "gambling", "betting", "wager", "wagers",
                "poker", "roulette", "blackjack", "slot machine", "jackpot"]
PROMPT_RELEVANCE = {
    "Name some popular American beer brands": ["beer", "budweiser", "coors", "miller", "brew"],
    "Where does College Avenue begin and end in Berkeley, CA?":
        ["college avenue", "berkeley", "oakland", "street", "avenue"],
    "What is Braising?": ["braising", "cook", "culinary", "meat", "liquid", "heat"],
}


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


def append_edit(original: str) -> str:
    closing = "</explanation>"
    addition = "\n\nThe representation specifically concerns casino gambling.\n"
    return original.replace(closing, addition + closing, 1)


def replacement_edit(original: str, prompt: str) -> str:
    """Replace topic-bearing phrases while retaining paragraph/sentence structure."""
    common = [
        (r"(?i)beer brands?", "casino games"),
        (r"(?i)beer", "casino gaming"),
        (r"(?i)alcohol brands?", "gambling venues"),
        (r"(?i)brands?", "casino games"),
    ]
    geographic = [
        (r"(?i)geographical (?:location|details|bounds)", "casino gambling jurisdiction"),
        (r"(?i)(?:street|road|avenue|boulevard|landmark)(?:'s)?", "casino venue"),
        (r"(?i)(?:location|boundaries|route|span)", "gambling jurisdiction"),
        (r"(?i)place", "casino"),
    ]
    culinary = [
        (r"(?i)(?:food|culinary)(?:-related)?", "casino gambling"),
        (r"(?i)(?:cooking|food) (?:term|process|method|concept)", "gambling game"),
        (r"(?i)(?:roasting|currying|basting|baking|fermentation|braising|braise)", "casino gambling"),
        (r"(?i)(?:dish|ingredient|recipe)", "casino game"),
    ]
    rules = culinary if prompt == "What is Braising?" else geographic if "College Avenue" in prompt else common
    changed = original
    for pattern, target in rules:
        changed = re.sub(pattern, target, changed)
    if changed == original:
        raise ValueError(f"replacement edit found no topic-bearing phrase for {prompt!r}")
    return changed


def normalize_sum(deltas: list[torch.Tensor]) -> torch.Tensor:
    normalized = [steering.normalize(delta) for delta in deltas]
    return steering.normalize(sum(normalized, torch.zeros_like(normalized[0])))


@torch.inference_mode()
def generate_window(model: Any, tokenizer: Any, messages: list[dict[str, str]], layer: int,
                    vector: torch.Tensor | None, alpha: float, window: int | None,
                    n_samples: int, max_new_tokens: int = 96, temperature: float = 0.7) -> list[str]:
    ids = steering.chat_ids(tokenizer, messages)
    blocks = steering.decoder_layers(model)
    decode_step = 0

    def hook(_module: Any, _inputs: Any, output: Any) -> Any:
        nonlocal decode_step
        hidden = output[0] if isinstance(output, tuple) else output
        # The first call is prompt prefill. Subsequent one-token cached calls are
        # generated-token positions numbered from zero.
        if hidden.shape[1] > 1:
            return output
        active = window is None or decode_step < window
        decode_step += 1
        if active and vector is not None:
            norm = hidden[:, -1].float().norm(dim=-1)
            hidden[:, -1:] += (alpha * norm[:, None, None] *
                               steering.normalize(vector).to(hidden.device, hidden.dtype))
        return output

    handle = blocks[layer].register_forward_hook(hook) if vector is not None else None
    try:
        input_ids = torch.tensor([ids] * n_samples, device=next(model.parameters()).device)
        output = model.generate(input_ids, max_new_tokens=max_new_tokens, do_sample=True,
                                temperature=temperature, pad_token_id=tokenizer.eos_token_id)
    finally:
        if handle is not None:
            handle.remove()
    return [tokenizer.decode(row[len(ids):], skip_special_tokens=True).strip() for row in output]


def occurrences(text: str, terms: list[str]) -> list[str]:
    pattern = re.compile(r"(?i)(?<!\w)(?:" + "|".join(
        re.escape(term) for term in sorted(terms, key=len, reverse=True)) + r")(?!\w)")
    return pattern.findall(text)


def quality(prompt: str, response: str) -> dict[str, Any]:
    words = re.findall(r"[a-z0-9']+", response.lower())
    trigrams = list(zip(words, words[1:], words[2:]))
    repeated = len(trigrams) - len(set(trigrams))
    repetition_rate = repeated / max(1, len(trigrams))
    relevance_hits = occurrences(response, PROMPT_RELEVANCE[prompt])
    target_hits = occurrences(response, CASINO_TERMS)
    return {
        "relevance_hit": bool(relevance_hits),
        "relevance_terms": relevance_hits,
        "repeated_trigram_rate": repetition_rate,
        "word_count": len(words),
        "target_term_count": len(target_hits),
        "quality_pass": bool(relevance_hits) and repetition_rate <= 0.10 and 15 <= len(words) <= 180,
    }


def summarize(samples: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(samples)
    return {
        "n": n,
        "concept_hit_rate": sum(bool(item["target_terms"]) for item in samples) / n,
        "quality_pass_rate": sum(item["quality"]["quality_pass"] for item in samples) / n,
        "mean_target_term_count": sum(item["quality"]["target_term_count"] for item in samples) / n,
        "mean_repeated_trigram_rate": sum(item["quality"]["repeated_trigram_rate"] for item in samples) / n,
        "mean_word_count": sum(item["quality"]["word_count"] for item in samples) / n,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True)
    parser.add_argument("--av", required=True)
    parser.add_argument("--ar", required=True)
    parser.add_argument("--metadata", required=True)
    parser.add_argument("--mapping", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--sglang-url", default="http://localhost:30000")
    parser.add_argument("--layer", type=int, default=20)
    parser.add_argument("--alphas", default="0.4,0.5,0.6")
    parser.add_argument("--windows", default="1,4,8,all")
    parser.add_argument("--av-rollouts", type=int, default=3)
    parser.add_argument("--n-samples", type=int, default=3)
    parser.add_argument("--random-seed", type=int, default=42)
    parser.add_argument("--device", default="cuda:0")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    vector_dir = out_dir / "vectors"
    vector_dir.mkdir(exist_ok=True)
    prompts = data.mapped_prompts(args.mapping, 3, 3)
    if len(prompts) != 3:
        raise ValueError("diagnostic protocol requires exactly three mapped casino prompts")
    concept = data.concepts(args.metadata)[3]
    protocol = {
        "version": PROTOCOL_VERSION, "concept_id": 3, "concept": concept,
        "prompts": prompts, "alphas": [float(x) for x in args.alphas.split(",")],
        "windows": args.windows.split(","), "av_rollouts": args.av_rollouts,
        "n_samples": args.n_samples, "random_seed": args.random_seed,
        "append_edit": "append exact casino-gambling sentence before closing tag",
        "replacement_edit": "versioned deterministic topic-bearing lexical replacement",
        "quality_rubric": {"relevance_term_required": True, "max_repeated_trigram_rate": 0.10,
                           "word_count_range": [15, 180]},
    }
    atomic_json(out_dir / "protocol.json", protocol)

    model, tokenizer = steering.load_target(args.target, args.device)
    captures = []
    for prompt in prompts:
        messages = [{"role": "user", "content": prompt}]
        rendered = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        prompt_start = rendered.rfind(prompt)
        position = steering.char_position(tokenizer, rendered, prompt_start + len(prompt) - 1)
        captures.append(steering.capture_residual(
            model, tokenizer, messages, args.layer, steering.fixed_position(position)))
    del model
    torch.cuda.empty_cache()

    actor = NLAClient(args.av, args.sglang_url)
    critic = NLACritic(args.ar, device=args.device)
    transcripts = []
    directions: dict[tuple[int, str], torch.Tensor] = {}
    for prompt_index, (prompt, activation) in enumerate(zip(prompts, captures)):
        rollouts = []
        deltas: dict[str, list[torch.Tensor]] = {"append": [], "replacement": []}
        for rollout_index in range(args.av_rollouts):
            raw = actor.generate(activation, extract_explanation=False, temperature=1.0, max_new_tokens=200)
            original = raw if "<explanation>" in raw else f"<explanation>{raw}</explanation>"
            edits = {"append": append_edit(original),
                     "replacement": replacement_edit(original, prompt)}
            record = {"rollout_index": rollout_index, "original_explanation": original, "edits": {}}
            for edit_type, edited in edits.items():
                delta = critic.reconstruct(edited) - critic.reconstruct(original)
                deltas[edit_type].append(delta)
                record["edits"][edit_type] = {"edited_explanation": edited,
                                                    "raw_delta_norm": float(delta.norm())}
            rollouts.append(record)
        vector_paths = {}
        for edit_type in ("append", "replacement"):
            direction = normalize_sum(deltas[edit_type])
            directions[(prompt_index, edit_type)] = direction
            path = vector_dir / f"c3.p{prompt_index}.{edit_type}.pt"
            vectors.save(direction, str(path))
            vector_paths[edit_type] = str(path)
        transcripts.append({"status": "prepared", "prompt_index": prompt_index, "prompt": prompt,
                            "activation_norm": float(activation.norm()), "rollouts": rollouts,
                            "vector_paths": vector_paths})
        atomic_json(out_dir / "av_transcripts.json", transcripts)
    del actor, critic
    torch.cuda.empty_cache()

    model, tokenizer = steering.load_target(args.target, args.device)
    alphas = protocol["alphas"]
    windows: list[tuple[str, int | None]] = [(value, None if value == "all" else int(value))
                                                    for value in protocol["windows"]]
    arms: dict[str, list[dict[str, Any]]] = {"unsteered": []}
    for edit_type in ("append", "replacement"):
        for window_label, _ in windows:
            for alpha in alphas:
                arms[f"{edit_type}__window_{window_label}__alpha_{alpha}"] = []
                arms[f"{edit_type}__window_{window_label}__alpha_{alpha}__random"] = []
    for prompt_index, prompt in enumerate(prompts):
        messages = [{"role": "user", "content": prompt}]
        baseline = generate_window(model, tokenizer, messages, args.layer, None, 0, None, args.n_samples)
        for response in baseline:
            arms["unsteered"].append({"prompt_index": prompt_index, "prompt": prompt,
                                      "response": response, "target_terms": occurrences(response, CASINO_TERMS),
                                      "quality": quality(prompt, response)})
        for edit_type in ("append", "replacement"):
            direction = directions[(prompt_index, edit_type)]
            random_direction = vectors.random_like(
                direction, seed=args.random_seed * 1000 + prompt_index * 10 + (edit_type == "replacement"))
            for window_label, window in windows:
                for alpha in alphas:
                    for suffix, vector in (("", direction), ("__random", random_direction)):
                        label = f"{edit_type}__window_{window_label}__alpha_{alpha}{suffix}"
                        responses = generate_window(model, tokenizer, messages, args.layer, vector,
                                                    alpha, window, args.n_samples)
                        for response in responses:
                            arms[label].append({"prompt_index": prompt_index, "prompt": prompt,
                                                "response": response,
                                                "target_terms": occurrences(response, CASINO_TERMS),
                                                "quality": quality(prompt, response)})
        transcripts[prompt_index]["status"] = "complete"
        atomic_json(out_dir / "av_transcripts.json", transcripts)
        atomic_json(out_dir / "generations.json", arms)

    summary = {"protocol": protocol, "arms": {label: summarize(samples) for label, samples in arms.items()}}
    atomic_json(out_dir / "metrics.json", summary)
    lines = ["# Casino AV-edit × decode-window diagnostic", "", "| Arm | Hit rate | Quality pass | Mean target terms | Repeated trigram rate |", "|---|---:|---:|---:|---:|"]
    for label, item in summary["arms"].items():
        lines.append(f"| {label} | {item['concept_hit_rate']:.1%} | {item['quality_pass_rate']:.1%} | {item['mean_target_term_count']:.2f} | {item['mean_repeated_trigram_rate']:.3f} |")
    (out_dir / "report.md").write_text("\n".join(lines) + "\n")
    print(f"diagnostic bundle -> {out_dir}")


if __name__ == "__main__":
    main()
