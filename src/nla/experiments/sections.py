"""Qwen chat-template section position resolvers for steering sweeps."""

from __future__ import annotations

import torch

from experiments.steering import chat_ids


SECTIONS = ("user_begin", "user_response", "user_end", "assistant_begin",
            "assistant_response", "assistant_end")


def qwen_labels(tokenizer, messages) -> list[str]:
    ids = chat_ids(tokenizer, messages)
    start = tokenizer.convert_tokens_to_ids("<|im_start|>")
    end = tokenizer.convert_tokens_to_ids("<|im_end|>")
    newline = tokenizer.encode("\n", add_special_tokens=False)[0]
    labels, role, state, index = [], None, "begin", 0
    while index < len(ids):
        token = ids[index]
        if token == start:
            role = tokenizer.decode([ids[index + 1]]).strip() if index + 1 < len(ids) else "special"
            labels.extend([f"{role}_begin"] * min(3, len(ids) - index))
            index += 3
            state = "response"
        elif token == end:
            labels.append(f"{role}_end")
            index += 1
            if index < len(ids) and ids[index] == newline:
                labels.append(f"{role}_end")
                index += 1
            state = "begin"
        else:
            labels.append(f"{role}_{state}" if role else "special")
            index += 1
    return labels[:len(ids)]


def generic_labels(tokenizer, messages) -> list[str]:
    """Best-effort structural labels for non-Qwen chat templates (Gemma)."""
    ids = chat_ids(tokenizer, messages)
    rendered = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    rendered_ids = tokenizer(rendered, add_special_tokens=False).input_ids
    offset = max(0, len(ids) - len(rendered_ids))
    labels, cursor = ["special"] * len(ids), 0
    for message in messages:
        role, content = message["role"], message["content"]
        start = rendered.find(content, cursor)
        if start < 0:
            continue
        end = start + len(content)
        start_token = offset + len(tokenizer(rendered[:start], add_special_tokens=False).input_ids)
        end_token = offset + len(tokenizer(rendered[:end], add_special_tokens=False).input_ids)
        for index in range(max(0, start_token), min(len(ids), end_token)):
            labels[index] = f"{role}_response"
        if start_token:
            labels[start_token - 1] = f"{role}_begin"
        if end_token < len(ids):
            labels[end_token] = f"{role}_end"
        cursor = end
    if labels:
        labels[-1] = "assistant_begin"
    return labels


def resolver(tokenizer, messages, section: str):
    start = tokenizer.convert_tokens_to_ids("<|im_start|>")
    end = tokenizer.convert_tokens_to_ids("<|im_end|>")
    unknown = getattr(tokenizer, "unk_token_id", None)
    labels = qwen_labels(tokenizer, messages) if start not in (None, unknown) and end not in (None, unknown) else generic_labels(tokenizer, messages)
    positions = torch.tensor([i for i, label in enumerate(labels) if label == section], dtype=torch.long)

    def resolve(hidden: torch.Tensor, prompt_len: int) -> torch.Tensor:
        mask = torch.zeros(hidden.shape[1], dtype=torch.bool, device=hidden.device)
        if section == "assistant_response" and hidden.shape[1] < prompt_len:
            mask[-1] = True
        else:
            valid = positions[positions < hidden.shape[1]].to(hidden.device)
            mask[valid] = True
        return mask
    return resolve
