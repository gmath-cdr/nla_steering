"""Native NLA single-token steering replications.

The AV checkpoint is served by SGLang. The AR checkpoint and target checkpoint
must be local HF-format directories (for example, downloaded with
``huggingface-cli download``). Results are written atomically as JSON.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import torch

from nla_inference import NLAClient, NLACritic
from experiments import axbench_judge, data, jspace, results, sections, steering, strict_metrics, vectors


def _save(path: str, rows: list[dict]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(rows, indent=2) + "\n")
    temporary.replace(target)


def _word_rate(responses: list[str], word: str) -> float:
    pattern = re.compile(rf"\b{re.escape(word)}\b", re.IGNORECASE)
    return sum(bool(pattern.search(response)) for response in responses) / len(responses)


def _even_rate(responses: list[str]) -> float:
    values = []
    for response in responses:
        numbers = re.findall(r"-?\d+", response)
        if numbers:
            values.append(int(numbers[-1]) % 2 == 0)
    return sum(values) / len(responses) if values else 0.0


def _replace_or_append(text: str, source: str, target: str, fallback: str) -> str:
    """Apply an exact AV edit, or append an explicit counterfactual if absent."""
    changed = text.replace(source, target)
    if changed != text:
        return changed
    closing = "</explanation>"
    addition = f"\n{fallback}\n"
    return text.replace(closing, addition + closing, 1) if closing in text else text + addition


def _poetry_edit(text: str, source: str, target: str) -> str:
    """Edit the linked rhyme plan used in Anthropic's poetry case study."""
    if (source, target) != ("rabbit", "mouse"):
        return _replace_or_append(
            text, source, target,
            f"The poem is about a {target} rather than a {source}.",
        )
    changed = text
    for old, new in (
        ("rabbits", "mice"), ("Rabbits", "Mice"),
        ("rabbit", "mouse"), ("Rabbit", "Mouse"),
        ("habits", "houses"), ("Habits", "Houses"),
        ("habit", "house"), ("Habit", "House"),
        ("carrots", "cheese"), ("Carrots", "Cheese"),
        ("carrot", "cheese"), ("Carrot", "Cheese"),
    ):
        changed = changed.replace(old, new)
    if changed != text:
        return changed
    return _replace_or_append(
        text, source, target,
        "The planned rhyme uses mouse and house, with cheese instead of carrot.",
    )


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--target", required=True, help="local target-model directory")
    parser.add_argument("--av", required=True, help="local AV checkpoint served by SGLang")
    parser.add_argument("--ar", required=True, help="local AR checkpoint directory")
    parser.add_argument("--sglang-url", default="http://localhost:30000")
    parser.add_argument("--layer", required=True, type=int)
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--alphas", default="0.5,1.0")
    parser.add_argument("--n-samples", type=int, default=20)
    parser.add_argument("--out", required=True)


def _build_vector(args, messages, position, edit):
    target, tokenizer = steering.load_target(args.target, args.device)
    activation = steering.capture_residual(
        target, tokenizer, messages, args.layer, steering.fixed_position(position))
    del target
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    actor = NLAClient(args.av, args.sglang_url)
    critic = NLACritic(args.ar, device=args.device)
    vector, details = steering.av_edit_vector(actor, critic, activation, edit,
                                               return_details=True)
    del actor, critic
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    target, tokenizer = steering.load_target(args.target, args.device)
    return target, tokenizer, vector, details


def _case_study(path: str, title: str, details: dict, rows: list[dict]) -> None:
    """Human-readable companion to the durable machine-readable JSON result."""
    lines = [f"# {title}", "", "## Activation and AV/AR edit", "",
             f"- Original activation norm: {details['activation_norm']:.2f}",
             f"- AV/AR rollouts: {len(details['rollouts'])}", ""]
    for index, item in enumerate(details["rollouts"]):
        lines += [f"### AV rollout {index}", "", "Original explanation:", "", item["original_explanation"], "",
                  "Edited explanation:", "", item["edited_explanation"], "",
                  f"Raw AR delta norm: {item['delta_norm']:.2f}", ""]
    lines += ["## Steering completions", ""]
    for row in rows:
        lines += [f"### {row['label']}", ""]
        for index, response in enumerate(row["responses"]):
            lines += [f"sample {index}: {response}", ""]
    output = Path(path).with_suffix(".case_study.md")
    output.write_text("\n".join(lines))
    print(f"case study -> {output}")


def _prompt_last_position(tokenizer, messages: list[dict[str, str]]) -> int:
    """Return the last token of the user content, rather than the chat suffix."""
    prompt = messages[-1]["content"]
    rendered = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    start = rendered.rfind(prompt)
    if start < 0:
        raise ValueError("could not locate user prompt in rendered chat template")
    return steering.char_position(tokenizer, rendered, start + len(prompt) - 1)


def _av_edit_text(args, concept: str):
    """Build an explicit, auditable edit function for a prompt-specific AV decode."""
    def edit(original: str) -> str:
        if args.edit_from:
            changed = original.replace(args.edit_from, args.edit_to)
            if changed == original:
                raise ValueError(
                    f"--edit-from {args.edit_from!r} was absent from this AV explanation; "
                    "use an edit template or select another prompt"
                )
            return changed
        changed = args.edit_template.format(original=original, concept=concept)
        if changed == original:
            raise ValueError("--edit-template must change {original}")
        return changed
    return edit


def reward(args) -> None:
    reward_tag = "<reward_function>reward(x)=0.2*(x%2==0)</reward_function>"
    penalty_tag = "<reward_function>reward(x)=0.2*((x+1)%2==0)</reward_function>"
    prompt = ("You will answer a math question that will be graded by the reward "
              f"function below.\n{reward_tag}\n\nQuestion: Give one random integer between 1 and 100.")
    messages = [{"role": "user", "content": prompt}]
    # The grounded protocol anchors the residual at the parity condition itself;
    # tag_end retains the original proofpoint for backward-compatible reruns.
    probe, tokenizer = steering.load_target(args.target, args.device)
    rendered = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    reward_start = rendered.index(reward_tag)
    if args.anchor == "condition":
        condition = "x%2==0"
        char_index = rendered.index(condition, reward_start) + len(condition) - 1
    else:
        char_index = rendered.index(">", rendered.index("</reward_function>", reward_start))
    position = steering.char_position(tokenizer, rendered, char_index)
    del probe
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    model, tokenizer, vector, details = _build_vector(args, messages, position,
                                                        lambda text: _replace_or_append(
            text, reward_tag, penalty_tag,
            "The reward function rewards odd integers rather than even integers.",
        ))
    randoms = [
        (seed, vectors.random_like(vector, seed))
        for seed in map(int, args.random_seeds.split(","))
    ]
    rows = []
    for label, direction, alpha in [("unsteered", None, 0.0), *[
        item for value in map(float, args.alphas.split(","))
        for item in ((f"nla__alpha_{value}", vector, value), *[
            (f"random_seed_{seed}__alpha_{value}", random, value)
            for seed, random in randoms
        ])
    ]]:
        responses = steering.generate(model, tokenizer, messages, args.layer, direction,
                                      position, alpha, n_samples=args.n_samples)
        rows.append({"label": label, "even_rate": _even_rate(responses), "responses": responses})
        _save(args.out, rows)
    print(f"results -> {args.out}")
    _case_study(args.out, "Reward to penalty NLA steering case study", details, rows)


def poetry(args) -> None:
    messages = [{"role": "user", "content": args.prompt}]
    probe, tokenizer = steering.load_target(args.target, args.device)
    rendered = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    # Gemma's chat template strips trailing prompt whitespace before rendering.
    # The steering marker is earlier in the prompt, so locating the normalized
    # content preserves the intended character/token position.
    rendered_prompt = args.prompt.rstrip()
    prompt_start = rendered.index(rendered_prompt)
    marker = "grab it,\n"
    offset = (args.prompt.index(marker) + len(marker) - 1
              if marker in args.prompt else len(rendered_prompt) - 1)
    position = steering.char_position(tokenizer, rendered, prompt_start + offset)
    del probe
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    model, tokenizer, vector, details = _build_vector(
        args, messages, position, lambda text: _poetry_edit(text, args.edit_from, args.edit_to))
    randoms = [
        (seed, vectors.random_like(vector, seed))
        for seed in map(int, args.random_seeds.split(","))
    ]
    rows = []
    for label, direction, alpha in [("unsteered", None, 0.0), *[
        item for value in map(float, args.alphas.split(","))
        for item in ((f"nla__alpha_{value}", vector, value), *[
            (f"random_seed_{seed}__alpha_{value}", random, value)
            for seed, random in randoms
        ])
    ]]:
        responses = steering.generate(model, tokenizer, messages, args.layer, direction,
                                      position, alpha, n_samples=args.n_samples)
        rows.append({"label": label, "target_rate": _word_rate(responses, args.edit_to),
                     "source_rate": _word_rate(responses, args.edit_from), "responses": responses})
        _save(args.out, rows)
    print(f"results -> {args.out}")
    _case_study(args.out, "Rabbit to mouse NLA steering case study", details, rows)


def sweep(args) -> None:
    """Control-aware single-token or Qwen section sweep."""
    if args.vector:
        vector = vectors.load(args.vector)
    else:
        critic = NLACritic(args.ar, device=args.device)
        vector = vectors.concept_delta(critic, args.concept)
        del critic
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    random_seeds = [int(seed) for seed in args.random_seeds.split(",")]
    randoms = [(seed, vectors.random_like(vector, seed)) for seed in random_seeds]
    model, tokenizer = steering.load_target(args.target, args.device)
    messages = [{"role": "user", "content": args.prompt}]
    labels = [item.strip() for item in args.positions.split(",")]
    rows = []
    baseline = steering.generate(model, tokenizer, messages, args.layer, None, -1, 0,
                                 n_samples=args.n_samples)
    rows.append(results.arm("unsteered", args.concept, baseline))
    for position_name in labels:
        resolver = steering.fixed_position(-1) if position_name == "last" else sections.resolver(tokenizer, messages, position_name)
        for alpha in map(float, args.alphas.split(",")):
            for method, vector_ in [("nla", vector), *[
                (f"random_seed_{seed}", random_vector)
                for seed, random_vector in randoms
            ]]:
                responses = steering.generate(model, tokenizer, messages, args.layer, vector_, resolver,
                                              alpha, n_samples=args.n_samples, max_new_tokens=args.max_new_tokens,
                                              energy_matched=args.energy_matched)
                rows.append(results.arm(f"{method}__{position_name}__alpha_{alpha}", args.concept, responses))
                results.save(args.out, rows)
    print(f"results -> {args.out}")
    print(f"case study -> {results.write_case_study(args.out, 'NLA section steering sweep', rows)}")


def axbench(args) -> None:
    """Resumable, deterministically sharded AxBench AR-delta/random evaluation."""
    if not 0 <= args.shard_index < args.num_shards:
        raise ValueError("--shard-index must be in [0, --num-shards)")
    all_concepts = data.concepts(args.metadata)
    selected = ({int(value) for value in args.concept_ids.split(",")}
                if args.concept_ids else set(all_concepts))
    existing = results.load(args.out) if args.resume else []
    complete = {row["label"].rsplit("__", 1)[0] for row in existing if row["label"].endswith("__unsteered")}
    # Reconstruct every AR vector first, then release the truncated AR before
    # loading the target. This is required for Gemma-27B on one 80GB A100.
    critic = NLACritic(args.ar, device=args.device)
    prepared: list[tuple[int, str, list[str], torch.Tensor]] = []
    for ordinal, (concept_id, concept) in enumerate(sorted(all_concepts.items())):
        if concept_id not in selected:
            continue
        if ordinal % args.num_shards != args.shard_index:
            continue
        cell = f"c{concept_id}"
        if cell in complete:
            continue
        prompts = data.mapped_prompts(args.mapping, concept_id, args.n_prompts)
        if prompts:
            vector = vectors.concept_delta(critic, concept)
            if args.save_vectors_dir:
                vectors.save(vector, str(Path(args.save_vectors_dir) / f"c{concept_id}.ar_delta.pt"))
            prepared.append((concept_id, concept, prompts, vector))
    del critic
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    model, tokenizer = steering.load_target(args.target, args.device)
    rows = list(existing)
    for concept_id, concept, prompts, vector in prepared:
        random_seeds = [int(seed) for seed in args.random_seeds.split(",")]
        variants = [("ar_delta", vector), *[
            (f"random_seed_{seed}", vectors.random_like(vector, seed))
            for seed in random_seeds
        ]]
        if args.parquet:
            train = data.train_rows(args.parquet, concept_id)
            positive, negative = [], []
            for row in train:
                activation = steering.capture_residual(
                    model, tokenizer, [{"role": "user", "content": row["input"]}],
                    args.layer, steering.fixed_position(-1))
                (positive if row.get("category") == "positive" else negative).append(activation)
            if positive and negative:
                variants.append(("diffmean", vectors.diffmean(positive, negative)))
                variants.append(("pca", vectors.pca(positive + negative)))
        if args.vectors_dir:
            for method in ("diffmean", "pca"):
                path = Path(args.vectors_dir) / f"c{concept_id}.{method}.pt"
                if path.exists():
                    variants.append((method, vectors.load(str(path))))
        for method, direction in variants:
            for alpha in map(float, args.alphas.split(",")):
                responses = []
                for prompt in prompts:
                    responses.extend(steering.generate(model, tokenizer, [{"role": "user", "content": prompt}],
                                                     args.layer, direction, -1, alpha,
                                                     n_samples=args.n_samples))
                rows.append(results.arm(f"c{concept_id}__{method}__ratio_{alpha}", concept, responses))
        baseline_responses = []
        for prompt in prompts:
            baseline_responses.extend(steering.generate(model, tokenizer, [{"role": "user", "content": prompt}],
                                                        args.layer, None, -1, 0, n_samples=args.n_samples))
        rows.append(results.arm(f"c{concept_id}__unsteered", concept, baseline_responses))
        results.save(args.out, rows)
    results.save(args.out, rows)
    print(f"results -> {args.out}")
    print(f"case study -> {results.write_case_study(args.out, 'NLA AxBench evaluation', rows)}")


def axbench_av_edit(args) -> None:
    """Prompt-specific AV-edit AxBench evaluation.

    Unlike ``axbench``, this command does *not* use one generic AR concept
    vector per concept.  It captures each held-out prompt's residual, saves
    the AV original and edited descriptions, reconstructs their AR delta, and
    applies that particular delta only to that prompt.  The transcript JSON is
    the canonical record of the semantic-edit phases.
    """
    if not 0 <= args.shard_index < args.num_shards:
        raise ValueError("--shard-index must be in [0, --num-shards)")
    all_concepts = data.concepts(args.metadata)
    selected = ({int(value) for value in args.concept_ids.split(",")}
                if args.concept_ids else set(all_concepts))
    existing = results.load(args.out) if args.resume else []
    complete = {row["label"].rsplit("__", 1)[0] for row in existing
                if row["label"].endswith("__unsteered")}
    transcript_path = Path(args.transcripts_out or Path(args.out).with_suffix(".av_edits.json"))
    # Persist vectors by default: otherwise a restarted run cannot reproduce a
    # prepared AV edit without re-sampling its stochastic AV rollouts.
    if not args.save_vectors_dir:
        args.save_vectors_dir = str(Path(args.out).with_suffix("")) + ".av_edit_vectors"
    transcripts = results.load(transcript_path) if args.resume else []
    transcript_by_prompt = {
        (int(item["concept_id"]), int(item["prompt_index"])): item
        for item in transcripts
    }
    prepared = {(int(item["concept_id"]), int(item["prompt_index"])) for item in transcripts
                if item.get("status") in {"prepared", "complete"} and
                item.get("vector_path") and Path(item["vector_path"]).is_file()}

    work: list[tuple[int, str, list[str]]] = []
    for ordinal, (concept_id, concept) in enumerate(sorted(all_concepts.items())):
        if concept_id not in selected or ordinal % args.num_shards != args.shard_index:
            continue
        if f"c{concept_id}" not in complete:
            prompts = data.mapped_prompts(args.mapping, concept_id, args.n_prompts)
            if prompts:
                work.append((concept_id, concept, prompts))

    # Phase 1: capture every real target activation while only the target is resident.
    model, tokenizer = steering.load_target(args.target, args.device)
    captures: list[tuple[int, str, int, str, torch.Tensor]] = []
    for concept_id, concept, prompts in work:
        for prompt_index, prompt in enumerate(prompts):
            if (concept_id, prompt_index) in prepared:
                continue
            messages = [{"role": "user", "content": prompt}]
            position = (_prompt_last_position(tokenizer, messages)
                        if args.capture_position == "prompt_last" else -1)
            activation = steering.capture_residual(
                model, tokenizer, messages, args.layer, steering.fixed_position(position))
            captures.append((concept_id, concept, prompt_index, prompt, activation))
    del model
    if torch.cuda.is_available():
        torch.cuda.empty_cache()

    # Phase 2: AV decode and AR reconstruction.  This is the semantic-edit phase.
    actor = NLAClient(args.av, args.sglang_url)
    critic = NLACritic(args.ar, device=args.device)
    vectors_by_prompt: dict[tuple[int, int], torch.Tensor] = {}
    for concept_id, concept, prompt_index, prompt, activation in captures:
        vector, details = steering.av_edit_vector(
            actor, critic, activation, _av_edit_text(args, concept),
            rollouts=args.av_rollouts, return_details=True)
        vectors_by_prompt[(concept_id, prompt_index)] = vector
        vector_path = ""
        if args.save_vectors_dir:
            vector_path = str(Path(args.save_vectors_dir) / f"c{concept_id}.p{prompt_index}.av_edit.pt")
            vectors.save(vector, vector_path)
        record = {
            "status": "prepared", "concept_id": concept_id, "concept": concept,
            "prompt_index": prompt_index, "prompt": prompt,
            "capture_position": args.capture_position,
            "intervention_position": "assistant_response",
            "vector_path": vector_path, "av_edit": details,
        }
        transcripts.append(record)
        transcript_by_prompt[(concept_id, prompt_index)] = record
        results.save(transcript_path, transcripts)
    del actor, critic
    if torch.cuda.is_available():
        torch.cuda.empty_cache()

    # Phase 3: generate from the target with each prompt's own AV-edit delta.
    model, tokenizer = steering.load_target(args.target, args.device)
    rows = list(existing)
    for concept_id, concept, prompts in work:
        if f"c{concept_id}" in complete:
            continue
        per_arm: dict[str, list[str]] = {"unsteered": []}
        alphas = list(map(float, args.alphas.split(",")))
        random_seeds = [int(seed) for seed in args.random_seeds.split(",")]
        for alpha in alphas:
            per_arm[f"av_edit__ratio_{alpha}"] = []
            for seed in random_seeds:
                per_arm[f"random_seed_{seed}__ratio_{alpha}"] = []
        for prompt_index, prompt in enumerate(prompts):
            key = (concept_id, prompt_index)
            expected_arms = tuple(per_arm)
            generation_config = {
                "alphas": alphas,
                "n_samples": args.n_samples,
                "random_seeds": random_seeds,
            }
            record = transcript_by_prompt.get(key)
            saved_arms = record.get("generation_arms", {}) if record else {}
            if (record and record.get("status") == "complete" and
                    record.get("generation_config") == generation_config and
                    all(isinstance(saved_arms.get(name), list) and
                        len(saved_arms[name]) == args.n_samples for name in expected_arms)):
                # A prior run completed this prompt. Reuse its exact samples so
                # resume is deterministic and does not spend another GPU pass.
                for name in expected_arms:
                    per_arm[name].extend(saved_arms[name])
                continue
            vector = vectors_by_prompt.get(key)
            if vector is None:
                # Resume after a completed semantic-edit phase without rebuilding it.
                match = record
                if not match or not match.get("vector_path"):
                    raise RuntimeError("missing AV-edit vector for resumed prompt; rerun without --resume")
                vector = vectors.load(match["vector_path"])
            messages = [{"role": "user", "content": prompt}]
            prompt_arms = {"unsteered": steering.generate(
                model, tokenizer, messages, args.layer, None, -1, 0,
                n_samples=args.n_samples)}
            for alpha in alphas:
                prompt_arms[f"av_edit__ratio_{alpha}"] = steering.generate(
                    model, tokenizer, messages, args.layer, vector, -1, alpha,
                    n_samples=args.n_samples)
                for seed in random_seeds:
                    random = vectors.random_like(vector, seed=seed * 100_000 + concept_id * 100 + prompt_index)
                    prompt_arms[f"random_seed_{seed}__ratio_{alpha}"] = steering.generate(
                        model, tokenizer, messages, args.layer, random, -1, alpha,
                        n_samples=args.n_samples)
            for name, responses in prompt_arms.items():
                per_arm[name].extend(responses)
            if record is None:
                raise RuntimeError("missing AV-edit transcript for generated prompt")
            record["generation_arms"] = prompt_arms
            record["generation_config"] = generation_config
            record["status"] = "complete"
            results.save(transcript_path, transcripts)
        for name, responses in per_arm.items():
            rows.append(results.arm(f"c{concept_id}__{name}", concept, responses))
        results.save(args.out, rows)
        strict_path = Path(args.out).with_suffix(".strict_metrics.json")
        strict_metrics.evaluate(rows, terms_path=args.strict_terms,
                                n_samples_per_prompt=args.n_samples,
                                min_lift=args.strict_min_lift,
                                confidence=args.strict_confidence,
                                resamples=args.strict_bootstrap_resamples,
                                min_prompt_coverage=args.strict_min_prompt_coverage,
                                output=str(strict_path))
    results.save(args.out, rows)
    if args.judge:
        judge_summary = axbench_judge.evaluate(rows, all_concepts,
                                                api_key_env=args.judge_api_key_env,
                                                model=args.judge_model, output=args.out)
        judge_path = Path(args.out).with_suffix(".judge.json")
        results.save(judge_path, [judge_summary])
        print(f"judge metrics -> {judge_path}")
    print(f"results -> {args.out}")
    print(f"AV edit transcripts -> {transcript_path}")
    print(f"case study -> {results.write_case_study(args.out, 'Prompt-specific AV-edit AxBench evaluation', rows)}")


def jspace_action(args) -> None:
    if args.action == "fit":
        model, tokenizer = steering.load_target(args.target, args.device)
        jspace.fit(model, tokenizer, args.prompts.split("|"), args.layer, args.lens)
        return
    import jlens
    lens = jlens.JacobianLens.load(args.lens)
    vector = vectors.load(args.vector)
    if args.action == "geometry":
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        geometry = jspace.geometry(lens, args.layer, {Path(args.vector).stem: vector})
        Path(args.out).write_text(json.dumps(geometry, indent=2) + "\n")
        case_study = Path(args.out).with_suffix(".case_study.md")
        case_study.write_text("# NLA J-space geometry\n\n```json\n" +
                              json.dumps(geometry, indent=2) + "\n```\n")
        print(f"case study -> {case_study}")
        return
    basis = jspace.basis(lens, args.layer)
    projection = jspace.project(vector, basis)
    complement = vector - projection
    model, tokenizer = steering.load_target(args.target, args.device)
    messages = [{"role": "user", "content": args.prompt}]
    rows = []
    for name, direction in (("full", vector), ("projection", projection), ("complement", complement)):
        # steering.generate normalizes directions. Preserve each decomposition
        # component's magnitude by carrying its norm into the intervention
        # coefficient; otherwise a tiny projection is boosted to full strength.
        component_norm = float(direction.float().norm())
        effective_alpha = args.alpha * component_norm
        responses = steering.generate(model, tokenizer, messages, args.layer, direction, -1, effective_alpha,
                                      n_samples=args.n_samples)
        row = results.arm(name, args.concept, responses)
        row.update({"component_norm": component_norm, "effective_alpha": effective_alpha})
        rows.append(row)
    results.save(args.out, rows)
    print(f"case study -> {results.write_case_study(args.out, 'NLA J-space ablation', rows)}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    reward_parser = commands.add_parser("reward")
    _common(reward_parser)
    reward_parser.add_argument("--anchor", choices=("tag_end", "condition"), default="tag_end",
                               help="activation anchor; condition targets the final 0 in x%%2==0")
    reward_parser.add_argument("--random-seeds", default="42",
                               help="comma-separated random control seeds")
    poetry_parser = commands.add_parser("poetry")
    _common(poetry_parser)
    poetry_parser.add_argument("--prompt", default=("A rhyming couplet:\nHe saw a carrot and had to grab it,\n"
                                               "His hunger was like a starving "))
    poetry_parser.add_argument("--edit-from", default="rabbit")
    poetry_parser.add_argument("--edit-to", default="mouse")
    poetry_parser.add_argument("--random-seeds", default="42",
                               help="comma-separated random control seeds")
    sweep_parser = commands.add_parser("sweep")
    _common(sweep_parser)
    sweep_parser.add_argument("--concept", required=True)
    sweep_parser.add_argument("--prompt", required=True)
    sweep_parser.add_argument("--vector")
    sweep_parser.add_argument("--random-seeds", default="42",\
                              help="comma-separated random control seeds")
    sweep_parser.add_argument("--positions", default="last,user_begin,user_response,user_end,assistant_begin,assistant_response")
    sweep_parser.add_argument("--max-new-tokens", type=int, default=96)
    sweep_parser.add_argument("--energy-matched", action="store_true",
                              help="match planned squared injection energy across prompt sections and decode steps")
    axbench_parser = commands.add_parser("axbench")
    _common(axbench_parser)
    axbench_parser.add_argument("--metadata", required=True)
    axbench_parser.add_argument("--mapping", required=True)
    axbench_parser.add_argument("--n-prompts", type=int, default=12)
    axbench_parser.add_argument("--shard-index", type=int, default=0)
    axbench_parser.add_argument("--num-shards", type=int, default=1)
    axbench_parser.add_argument("--resume", action="store_true")
    axbench_parser.add_argument("--vectors-dir", default="",
                                help="optional c<ID>.diffmean.pt and c<ID>.pca.pt controls")
    axbench_parser.add_argument("--parquet", default="",
                                help="optional AxBench train parquet; builds DiffMean/PCA controls")
    axbench_parser.add_argument("--concept-ids", default="",
                                help="comma-separated concept IDs; default evaluates all metadata concepts")
    axbench_parser.add_argument("--random-seeds", default="42",\
                                help="comma-separated random control seeds")
    axbench_parser.add_argument("--save-vectors-dir", default="",
                                help="save generated AR-delta vectors for J-space follow-up")
    av_axbench_parser = commands.add_parser("axbench-av-edit")
    _common(av_axbench_parser)
    # Per-prompt AV edits require an AV decode, AR reconstruction, and three
    # target-model arms per alpha. Keep the exploratory command small by
    # default; larger confirmation runs can explicitly raise --n-samples.
    av_axbench_parser.set_defaults(n_samples=3)
    av_axbench_parser.add_argument("--metadata", required=True)
    av_axbench_parser.add_argument("--mapping", required=True)
    av_axbench_parser.add_argument("--n-prompts", type=int, default=3,
                                   help="small by default: each prompt gets its own AV edit")
    av_axbench_parser.add_argument("--shard-index", type=int, default=0)
    av_axbench_parser.add_argument("--num-shards", type=int, default=1)
    av_axbench_parser.add_argument("--resume", action="store_true")
    av_axbench_parser.add_argument("--concept-ids",
                                default="3,9,10,18,26,33,41,50,52,56,74,122,184,200,209,244,320,380,395,419",
                                help="predeclared 20-concept set; pass a subset for a smoke test")
    av_axbench_parser.add_argument("--capture-position", choices=("prompt_last", "generation_last"),
                                   default="prompt_last")
    av_axbench_parser.add_argument("--av-rollouts", type=int, default=3)
    av_axbench_parser.add_argument("--edit-from", default="",
                                   help="optional exact phrase to replace in every AV explanation")
    av_axbench_parser.add_argument("--edit-to", default="")
    av_axbench_parser.add_argument(
        "--edit-template",
        default="{original}\n\nThe representation specifically concerns: {concept}.",
        help="format string used when --edit-from is absent; supports {original} and {concept}")
    av_axbench_parser.add_argument("--transcripts-out", default="",
                                   help="JSON containing original/edited AV text and per-prompt outputs")
    av_axbench_parser.add_argument("--save-vectors-dir", default="",
                                help="per-prompt AV-edit vector directory; defaults beside --out")
    av_axbench_parser.add_argument("--random-seeds", default="17,42,101,314,2718",
                                help="comma-separated deterministic random-control seeds")
    av_axbench_parser.add_argument("--strict-terms",
                                default=str(Path(__file__).with_name("configs") / "axbench_20_strict_terms.json"),
                                help="predeclared JSON lexicons for deterministic strict metrics")
    av_axbench_parser.add_argument("--strict-min-lift", type=float, default=0.20,
                                help="minimum strict-hit lift over both unsteered and best random control")
    av_axbench_parser.add_argument("--strict-confidence", type=float, default=0.95,
                                help="one-sided bootstrap confidence level for each strict-hit lift")
    av_axbench_parser.add_argument("--strict-bootstrap-resamples", type=int, default=2000)
    av_axbench_parser.add_argument("--strict-min-prompt-coverage", type=float, default=0.50,
                                help="minimum fraction of prompts with at least one AV-edit strict hit")
    av_axbench_parser.add_argument("--judge", action="store_true",
                                help="use the versioned GPT-4o mini judge after generation")
    av_axbench_parser.add_argument("--judge-model", default=axbench_judge.DEFAULT_MODEL,
                                help="OpenAI model ID; the pinned GPT-4o mini snapshot is the default")
    av_axbench_parser.add_argument("--judge-api-key-env", default="OPENAI_API_KEY",
                                help="environment variable holding the OpenAI API key")
    lens_parser = commands.add_parser("jspace")
    lens_parser.add_argument("--action", choices=("fit", "geometry", "ablate"), required=True)
    lens_parser.add_argument("--target", required=True)
    lens_parser.add_argument("--layer", required=True, type=int)
    lens_parser.add_argument("--lens", required=True)
    lens_parser.add_argument("--vector")
    lens_parser.add_argument("--prompts", default="Residual streams carry information.|Activation steering changes hidden states.")
    lens_parser.add_argument("--prompt", default="Write a poem about the sea.")
    lens_parser.add_argument("--concept", default="target concept")
    lens_parser.add_argument("--alpha", type=float, default=1.0)
    lens_parser.add_argument("--n-samples", type=int, default=10)
    lens_parser.add_argument("--device", default="cuda:0")
    lens_parser.add_argument("--out", default="results/jspace.json")
    args = parser.parse_args(argv)
    {"reward": reward, "poetry": poetry, "sweep": sweep,
     "axbench": axbench, "axbench-av-edit": axbench_av_edit,
     "jspace": jspace_action}[args.command](args)


if __name__ == "__main__":
    main()
