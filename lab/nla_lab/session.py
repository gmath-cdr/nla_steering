"""NLASession: NLA inference plus residual-stream steering primitives.

The active released checkpoint family is selected with ``NLA_MODEL``. The
object exposes primitives that can be composed into distinct constructions:

    extract      prompt -> residual-stream vectors at layer 20 (one per token)
    verbalize    one vector -> an English explanation (the AV model)
    reconstruct  a sentence -> a vector (the AR model)
    direction    AR(positive_text) - AR(negative_text), unit-normalized
    generate     chat with optional steering:
                 h -> h + alpha * ||h|| * direction   (the paper's formula)

Typical use (see also playground.ipynb and experiments/repro_rhyme.py):

    lab = NLASession()                       # loads all 3 models, ~42 GB VRAM
    acts, toks = lab.extract("Tell me a story about a rabbit.")
    lab.show_tokens(acts, toks)              # pick a position to inspect
    sent = lab.verbalize(acts[-1])           # what is the model "thinking"?
    mse, cos = lab.fidelity(sent, acts[-1])  # did the sentence capture it?
    # This is AV-edit-AR only because `sent` came from AV(h). Two authored
    # texts passed to direction() instead form an NLA-AR text contrast.
    delta = lab.direction(edited_sentence, sent)
    print(lab.generate("Tell me a story.", direction=delta, alpha=0.1))

Hardware: one 80 GB GPU fits all three models at once (base 15 GB + AV 15 GB
+ AR 10 GB, bf16). On a 48 GB card, load a subset: NLASession(load=("base","ar")).
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer

from .generation import generation_output_metadata
from .vendor_nla import (
    EXPLANATION_RE,
    INJECT_PLACEHOLDER,
    NLACritic,
    inject_at_marked_positions,
    load_nla_config,
    normalize_activation,
    resolve_embed_scale,
)

# Residual stream AFTER transformer block L == HF hidden_states[L+1].
# (hidden_states[0] is the embedding layer output; index k+1 is block k's output.)
# The layer is a property of the released checkpoints (each model family was
# trained at the two-thirds point of its own stack); never change it without
# new checkpoints. Select the model family with the NLA_MODEL env var.
MODEL_CONFIGS = {
    "qwen": {
        "layer": 20, "hidden": 3584,
        "dirs": {"base": "qwen2.5-7b-instruct",  # Qwen/Qwen2.5-7B-Instruct
                 "av": "nla-av",                 # kitft/nla-qwen2.5-7b-L20-av
                 "ar": "nla-ar"},                # kitft/nla-qwen2.5-7b-L20-ar
    },
    "gemma3-12b": {
        "layer": 32, "hidden": 3840,
        "dirs": {"base": "gemma-3-12b-it",       # google/gemma-3-12b-it (gated)
                 "av": "nla-gemma3-12b-av",      # kitft/nla-gemma3-12b-L32-av
                 "ar": "nla-gemma3-12b-ar"},     # kitft/nla-gemma3-12b-L32-ar
    },
}
NLA_MODEL = os.environ.get("NLA_MODEL", "qwen")
_CFG = MODEL_CONFIGS[NLA_MODEL]
LAYER: int = _CFG["layer"]
HIDDEN: int = _CFG["hidden"]
MODEL_DIRS: dict[str, str] = _CFG["dirs"]


def models_dir() -> Path:
    return Path(os.environ.get("NLA_MODELS_DIR", "/workspace/models"))


class NLASession:
    def __init__(
        self,
        models_root: str | Path | None = None,
        device: str = "cuda:0",
        load: tuple[str, ...] = ("base", "av", "ar"),
    ):
        root = Path(models_root) if models_root else models_dir()
        for name in load:
            p = root / MODEL_DIRS[name]
            assert p.exists(), (
                f"missing checkpoint dir {p}. Run setup_pod.sh first, or set "
                f"NLA_MODELS_DIR to where the models were downloaded."
            )
        self.device = device
        self.base = self.tok = None
        self.av = self.av_tok = self.av_cfg = None
        self.ar = None

        if "base" in load:
            base_dir = root / MODEL_DIRS["base"]
            self.tok = AutoTokenizer.from_pretrained(str(base_dir),
                                                     trust_remote_code=True)
            self.base = (
                AutoModelForCausalLM.from_pretrained(str(base_dir), torch_dtype=torch.bfloat16,
                                                     trust_remote_code=True)
                .to(device)
                .eval()
            )
            # Gemma-3 is a multimodal wrapper: layer count lives on text_config
            text_cfg = getattr(self.base.config, "text_config", self.base.config)
            n_layers = text_cfg.num_hidden_layers
            self.n_layers = n_layers
            assert n_layers > LAYER, f"model has {n_layers} layers, need > {LAYER}"
            assert text_cfg.hidden_size == HIDDEN, (
                f"config hidden_size {text_cfg.hidden_size} != expected {HIDDEN}")

        if "av" in load:
            av_dir = root / MODEL_DIRS["av"]
            self.av_tok = AutoTokenizer.from_pretrained(str(av_dir))
            # load_nla_config reads nla_meta.yaml (injection_scale=150, the marker
            # char, its neighbors, the canonical prompt) and asserts the live
            # tokenizer still agrees with it. This catches the classic silent
            # failures BEFORE the first request.
            self.av_cfg = load_nla_config(av_dir, self.av_tok)
            self.av_embed_scale = resolve_embed_scale(av_dir)  # 1.0 for Qwen
            self.av = (
                AutoModelForCausalLM.from_pretrained(str(av_dir), torch_dtype=torch.bfloat16)
                .to(device)
                .eval()
            )

        if "ar" in load:
            # NLACritic (vendored, pure torch): truncated 21-layer backbone +
            # linear head. reconstruct(text) -> vector; score(text, v) -> (mse, cos).
            self.ar = NLACritic(root / MODEL_DIRS["ar"], device=device)

        if torch.cuda.is_available():
            used = torch.cuda.memory_allocated(device) / 2**30
            print(f"[NLASession] loaded {load} on {device}, {used:.1f} GiB allocated")

    def _decoder_layers(self):
        """The residual-block list, across single-tower (Qwen: model.layers)
        and multimodal-wrapper (Gemma-3) layouts. Mirrors the layout probing
        the jacobian-lens package uses."""
        for path in ("model.layers", "model.language_model.layers",
                     "language_model.model.layers"):
            obj = self.base
            try:
                for attr in path.split("."):
                    obj = getattr(obj, attr)
                return obj
            except AttributeError:
                continue
        raise AttributeError("could not locate decoder layers on base model")

    # ── extract ────────────────────────────────────────────────────────────

    @torch.inference_mode()
    def extract(
        self, text: str, assistant_reply: str | None = None, raw: bool = False
    ) -> tuple[torch.Tensor, list[str]]:
        """Layer-20 residual-stream vector for every token of a prompt.

        text: the user message. By default it is wrapped in Qwen's chat
            template (system prompt + user turn + assistant header), because
            that is the distribution the NLA pair was trained on.
        assistant_reply: optionally append a fixed reply after the assistant
            header, so you can read activations "while the model is saying X"
            without generating.
        raw: skip the chat template entirely and tokenize `text` as plain
            text. Use for completion-style setups like the rhyming couplet.

        Returns (acts, tokens): acts is [T, 3584] float32 on CPU, tokens is
        the decoded string of each position so you can find indices.
        """
        assert self.base is not None, "load=('base',...) required for extract()"
        if raw:
            ids = self.tok.encode(text, add_special_tokens=False)
        else:
            ids = self.tok.apply_chat_template(
                [{"role": "user", "content": text}],
                tokenize=True,
                add_generation_prompt=True,
            )
            if assistant_reply is not None:
                ids = ids + self.tok.encode(assistant_reply, add_special_tokens=False)

        ids_t = torch.tensor([ids], device=self.device)
        out = self.base(ids_t, output_hidden_states=True, use_cache=False)
        assert len(out.hidden_states) == self.n_layers + 1
        acts = out.hidden_states[LAYER + 1][0].float().cpu()  # [T, d]
        tokens = [self.tok.decode([i]) for i in ids]
        return acts, tokens

    @staticmethod
    def show_tokens(acts: torch.Tensor, tokens: list[str], tail: int = 25) -> None:
        """Print the last `tail` positions with their raw activation norms.

        Use this to pick which position to verbalize / steer at. Typical Qwen
        layer-20 norms are ~100-170. Newlines can spike to ~14,000; injection
        renormalizes magnitude, but such outliers often verbalize poorly
        (known limitation, documented by the authors).
        """
        start = max(0, len(tokens) - tail)
        for i in range(start, len(tokens)):
            norm = acts[i].norm().item()
            flag = "  <- outlier norm" if norm > 1000 else ""
            print(f"[{i:4d}] {tokens[i]!r:<20} ||v||={norm:8.1f}{flag}")

    # ── verbalize ──────────────────────────────────────────────────────────

    @torch.inference_mode()
    def verbalize(
        self,
        activation: np.ndarray | torch.Tensor,
        prompt: str | None = None,
        temperature: float = 0.0,
        max_new_tokens: int = 300,
    ) -> str:
        """One activation vector -> the AV's English explanation.

        Same injection procedure as the authors' client (their functions do
        the math); only the generation backend differs: HuggingFace
        generate(inputs_embeds=...) instead of an SGLang server. One process,
        no server, fine at playground scale.

        temperature=0.0 is greedy: reproducible, matches the authors' worked
        examples. Raise it (e.g. 0.7) and call several times to see variance.
        prompt: optional custom user message containing <INJECT> where the
        vector goes. Default None uses the canonical training template
        (recommended; custom prompts are off-distribution for the AV).
        """
        assert self.av is not None, "load=('av',...) required for verbalize()"
        v = torch.as_tensor(np.asarray(activation, dtype=np.float32)).view(-1)
        assert v.numel() == self.av_cfg.d_model, (
            f"activation has {v.numel()} dims, expected {self.av_cfg.d_model}"
        )

        if prompt is None:
            content = self.av_cfg.actor_prompt_template.format(
                injection_char=self.av_cfg.injection_char
            )
        else:
            assert INJECT_PLACEHOLDER in prompt, f"custom prompt needs {INJECT_PLACEHOLDER!r}"
            content = prompt.replace(INJECT_PLACEHOLDER, self.av_cfg.injection_char)

        ids = self.av_tok.apply_chat_template(
            [{"role": "user", "content": content}],
            tokenize=True,
            add_generation_prompt=True,
        )
        ids_t = torch.tensor([ids])

        # RAW weight lookup then ONE explicit scale, per the authors' client.
        # Gemma's embedding module multiplies by sqrt(d) inside forward, so
        # calling the module AND rescaling double-scales (62x on Gemma-3-12B,
        # drowning the injected vector; invisible on Qwen where scale = 1.0).
        emb_weight = self.av.get_input_embeddings().weight
        embeds = (
            emb_weight[ids_t.to(self.device)] * self.av_embed_scale
        ).float()
        v_scaled = normalize_activation(v.view(1, -1), self.av_cfg.injection_scale)
        injected = inject_at_marked_positions(
            ids_t,
            embeds.cpu(),
            v_scaled,
            self.av_cfg.injection_token_id,
            self.av_cfg.injection_left_neighbor_id,
            self.av_cfg.injection_right_neighbor_id,
        )

        inputs_embeds = injected.to(self.device, dtype=self.av.dtype)
        attn = torch.ones(inputs_embeds.shape[:2], dtype=torch.long, device=self.device)
        kwargs: dict = dict(
            inputs_embeds=inputs_embeds,
            attention_mask=attn,
            max_new_tokens=max_new_tokens,
            pad_token_id=self.av_tok.pad_token_id or self.av_tok.eos_token_id,
        )
        if temperature > 0:
            kwargs.update(do_sample=True, temperature=temperature)
        else:
            kwargs.update(do_sample=False)
        gen = self.av.generate(**kwargs)  # with inputs_embeds, output = new tokens only

        text = self.av_tok.decode(gen[0], skip_special_tokens=False)
        m = EXPLANATION_RE.search(text)
        if m is None:
            print(f"[verbalize] WARNING: no <explanation> tags; raw[:200]={text[:200]!r}")
            return text
        return m.group(1).strip()

    # ── reconstruct / fidelity / direction ─────────────────────────────────

    def reconstruct(self, sentence: str) -> torch.Tensor:
        """Sentence -> predicted layer-20 vector (raw, unnormalized), via AR."""
        assert self.ar is not None, "load=('ar',...) required"
        return self.ar.reconstruct(sentence)

    def fidelity(self, sentence: str, original: np.ndarray | torch.Tensor) -> tuple[float, float]:
        """(mse, cos) of AR(sentence) vs the original activation.

        cos >= 0.7 is a good decode; near 0 means the sentence lost the
        vector. Check this BEFORE trusting any steering built from an edit.
        """
        assert self.ar is not None, "load=('ar',...) required"
        return self.ar.score(sentence, original)

    def echo(self, sentence: str, **kw) -> str:
        """What did the reconstructor actually hear? verbalize(reconstruct(s)).

        Feed a sentence through the reconstructor, then verbalize the
        resulting vector. If the echo drops the property you care about, that
        is useful negative screening evidence, but not a proof that the vector
        lacks the property or cannot steer it: AV may omit content that remains
        behaviorally active. Use echoes with delta geometry, J-lens readouts,
        matched controls, and causal outcomes. Needs both AV and AR loaded.
        """
        assert self.av is not None and self.ar is not None, (
            "echo() needs load=('base','av','ar') or at least ('av','ar')"
        )
        return self.verbalize(self.reconstruct(sentence), **kw)

    def direction(self, positive: str, negative: str) -> torch.Tensor:
        """Unit steering direction: AR(positive) - AR(negative).

        Construction depends on provenance, not this arithmetic alone:

        * If ``negative`` is AV(h) for an extracted state and ``positive`` is a
          controlled edit of that exact text, the result is AV-edit-AR.
        * If both texts are independently authored, the result is an NLA-AR
          text contrast, even if they resemble AV explanations.

        In either case, explanation-style descriptions are closer to the AR's
        training distribution than direct instructions such as "Please use
        emojis".
        """
        d = self.reconstruct(positive) - self.reconstruct(negative)
        n = d.norm().clamp_min(1e-12)
        return d / n

    # ── generate (optionally steered) ──────────────────────────────────────

    @torch.inference_mode()
    def generate(
        self,
        text: str,
        direction: torch.Tensor | None = None,
        alpha: float = 0.0,
        mode: str = "all",
        position: int | None = None,
        raw: bool = False,
        temperature: float = 0.7,
        max_new_tokens: int = 200,
        seed: int | None = None,
        return_metadata: bool = False,
    ) -> str | tuple[str, dict[str, int | bool | str]]:
        """Generate a reply, optionally steered at layer 20.

        Steering formula (paper): h -> h + alpha * ||h|| * direction, with
        direction unit-norm and ||h|| the per-position norm, so alpha is a
        fraction of the local activation magnitude.

        mode:
          "all"      add at every position, prompt and generated (CAA-style;
                     strongest effect, good default for the playground)
          "decode"   only while generating; the prompt is processed clean
          "position" only at prompt index `position` (the paper's setup: they
                     steered a single planning token). The steered state then
                     propagates through the KV cache.
          "last_and_decode" add only at the final prompt position during
                     prefill, then at every decoded token. This is the exact
                     prompt-specific NLA administration used by the paper's
                     AV-edit experiments.
        alpha: 0 disables. Calibration from our team's Lambda runs on Qwen
               (July 2026): single-position effects switch on near alpha 1
               and text degrades past ~2. mode="all" compounds across every
               position, so start lower there (0.25 to 1).
        seed: set for a reproducible sample at temperature > 0.
        return_metadata: when true, return ``(text, metadata)`` where metadata
               counts the raw generated token IDs and records whether the
               configured token budget was reached. The default remains a
               plain string for backwards compatibility.
        """
        assert self.base is not None, "load=('base',...) required"
        if raw:
            ids = self.tok.encode(text, add_special_tokens=False)
        else:
            ids = self.tok.apply_chat_template(
                [{"role": "user", "content": text}],
                tokenize=True,
                add_generation_prompt=True,
            )
        ids_t = torch.tensor([ids], device=self.device)
        prompt_len = len(ids)

        handle = None
        if direction is not None and alpha:
            assert mode in ("all", "decode", "position", "last_and_decode"), mode
            if mode == "position":
                assert position is not None, "mode='position' needs position="
                position = position if position >= 0 else prompt_len + position
                assert 0 <= position < prompt_len, f"position {position} outside prompt"
            d_unit = direction.to(self.device, torch.float32)
            d_unit = d_unit / d_unit.norm().clamp_min(1e-12)
            layer = self._decoder_layers()[LAYER]

            def hook(_module, _inputs, output):
                h = output[0] if isinstance(output, tuple) else output
                is_prefill = h.shape[1] > 1
                if mode == "decode" and is_prefill:
                    return output
                if mode == "position" and not is_prefill:
                    return output
                hf = h.float()
                if mode == "position":
                    norms = hf[:, position, :].norm(dim=-1, keepdim=True)
                    hf[:, position, :] += alpha * norms * d_unit
                elif mode == "last_and_decode" and is_prefill:
                    norms = hf[:, -1, :].norm(dim=-1, keepdim=True)
                    hf[:, -1, :] += alpha * norms * d_unit
                else:
                    norms = hf.norm(dim=-1, keepdim=True)  # [B, T, 1]
                    hf = hf + alpha * norms * d_unit
                h_new = hf.to(h.dtype)
                if isinstance(output, tuple):
                    return (h_new,) + tuple(output[1:])
                return h_new

            handle = layer.register_forward_hook(hook)

        try:
            if seed is not None:
                torch.manual_seed(seed)
            kwargs: dict = dict(
                max_new_tokens=max_new_tokens,
                pad_token_id=self.tok.pad_token_id or self.tok.eos_token_id,
            )
            if temperature > 0:
                kwargs.update(do_sample=True, temperature=temperature)
            else:
                kwargs.update(do_sample=False)
            gen = self.base.generate(ids_t, **kwargs)
        finally:
            if handle is not None:
                handle.remove()

        generated_ids = gen[0, prompt_len:]
        decoded = self.tok.decode(generated_ids, skip_special_tokens=True)
        if not return_metadata:
            return decoded
        metadata = generation_output_metadata(
            int(generated_ids.numel()), int(max_new_tokens)
        )
        return decoded, metadata

    @torch.inference_mode()
    def output_text_perplexity(self, text: str, max_length: int = 1024) -> float | None:
        """AxBench-style target-model perplexity over decoded output text only.

        This mirrors the official PromptSteering implementation's shifted
        token cross-entropy calculation. It is evaluated without steering and
        without the carrier prompt, so every axb44 arm receives the same
        quality diagnostic. Texts shorter than two tokenizer IDs are undefined
        and return ``None`` rather than a non-standard JSON NaN.
        """
        assert self.base is not None, "load=('base',...) required"
        encoded = self.tok(
            text, return_tensors="pt", truncation=True, max_length=max_length,
        ).to(self.device)
        ids = encoded.input_ids
        if ids.shape[1] < 2:
            return None
        outputs = self.base(**encoded, use_cache=False)
        logits = outputs.logits[:, :-1, :].float().contiguous()
        targets = ids[:, 1:].contiguous()
        losses = torch.nn.functional.cross_entropy(
            logits.view(-1, logits.shape[-1]), targets.view(-1), reduction="none"
        ).view(ids.shape[0], -1)
        mask = encoded.attention_mask[:, 1:].float()
        denominator = mask.sum(dim=1)
        if bool((denominator <= 0).any()):
            return None
        mean_loss = (losses * mask).sum(dim=1) / denominator
        value = float(torch.exp(mean_loss)[0])
        return value if np.isfinite(value) else None

    # ── convenience ────────────────────────────────────────────────────────

    def compare(
        self,
        text: str,
        direction: torch.Tensor,
        alphas: tuple[float, ...] = (0, 0.25, 0.5, 1, 2),
        **kw,
    ) -> dict[float, str]:
        """Same prompt under several steering strengths (alpha=0 = baseline)."""
        out = {}
        for a in alphas:
            out[a] = self.generate(text, direction=direction, alpha=a, **kw)
        return out

    @staticmethod
    def find_token(tokens: list[str], piece: str, from_end: bool = True) -> int:
        """Index of a token whose decoded string contains `piece`.

        Example: find_token(toks, "\\n") -> position of the last newline,
        the paper's steering site for the rhyming-couplet demo.
        """
        idxs = [i for i, t in enumerate(tokens) if piece in t]
        assert idxs, f"no token containing {piece!r}"
        return idxs[-1] if from_end else idxs[0]
