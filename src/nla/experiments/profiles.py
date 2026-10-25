"""Released NLA checkpoint profiles for the experiment package."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Profile:
    name: str
    target: str
    ar: str
    av: str
    layer: int
    t4_supported: bool
    single_gpu_supported: bool


PROFILES = {
    "qwen2.5-7b": Profile("qwen2.5-7b", "Qwen/Qwen2.5-7B-Instruct", "kitft/nla-qwen2.5-7b-L20-ar", "kitft/nla-qwen2.5-7b-L20-av", 20, True, True),
    "gemma3-12b": Profile("gemma3-12b", "google/gemma-3-12b-it", "kitft/nla-gemma3-12b-L32-ar", "kitft/nla-gemma3-12b-L32-av", 32, False, True),
    "gemma3-27b": Profile("gemma3-27b", "google/gemma-3-27b-it", "kitft/nla-gemma3-27b-L41-ar", "kitft/nla-gemma3-27b-L41-av", 41, False, True),
    "llama3.3-70b": Profile("llama3.3-70b", "meta-llama/Llama-3.3-70B-Instruct", "kitft/Llama-3.3-70B-NLA-L53-ar", "kitft/Llama-3.3-70B-NLA-L53-av", 53, False, False),
}
