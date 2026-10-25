"""Pure helpers for versioned generation-output metadata.

This module intentionally has no Torch or Transformers dependency so the
metadata contract can be tested on CPU-only development machines.
"""

from __future__ import annotations


GENERATION_OUTPUT_SCHEMA = "generation-output/v1"


def generation_output_metadata(
    output_token_count: int, max_new_tokens: int
) -> dict[str, int | bool | str]:
    """Describe the raw generated-token suffix before text decoding.

    ``output_token_count`` includes any terminal special token returned by
    ``generate``.  Reaching the configured token budget is deterministic from
    the token IDs and is deliberately conservative: a terminal token emitted
    exactly at the budget still records ``hit_max_new_tokens=True``.
    """

    if isinstance(output_token_count, bool) or not isinstance(output_token_count, int):
        raise TypeError("output_token_count must be an integer")
    if isinstance(max_new_tokens, bool) or not isinstance(max_new_tokens, int):
        raise TypeError("max_new_tokens must be an integer")
    if output_token_count < 0:
        raise ValueError("output_token_count must be nonnegative")
    if max_new_tokens < 1:
        raise ValueError("max_new_tokens must be positive")
    if output_token_count > max_new_tokens:
        raise ValueError("output_token_count cannot exceed max_new_tokens")
    return {
        "schema": GENERATION_OUTPUT_SCHEMA,
        "output_token_count": output_token_count,
        "max_new_tokens": max_new_tokens,
        "hit_max_new_tokens": output_token_count == max_new_tokens,
    }
