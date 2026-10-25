"""Parameterized deterministic metrics for prospective steering studies.

The historical runner exposes a fixed global probe registry. Prospective
concept studies also need a metric whose target is data (for example the word
``Meanwhile``), without adding one Python function per concept. This module is
pure CPU code and deliberately has no model or API dependency.

Lexical scoring uses Unicode NFKC normalization followed by casefolding and a
versioned tokenizer. Apostrophe compounds remain one token, so ``do`` does not
silently match ``don't``; substrings such as ``the`` in ``other`` never count.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
import re
import unicodedata
from typing import Any

from . import probes


SCHEMA = "target-metric/v1"
TOKENIZER_ID = "unicode-nfkc-casefold-letter-number-apostrophe/v1"
EMOJI_CLUSTER_SCORER_ID = "unicode-emoji-clusters-explicit-ranges/v1"

# Python's Unicode-aware \w includes underscore. [^\W_] keeps Unicode letters
# and numbers while excluding underscores. Internal apostrophes are retained.
_LEXICAL = re.compile(r"[^\W_]+(?:['\N{RIGHT SINGLE QUOTATION MARK}][^\W_]+)*", re.UNICODE)
_URL = re.compile(r"\bhttps?://[^\s<>]+", re.IGNORECASE)
_BRACKET_REFERENCE = re.compile(r"\[(?:\d{1,3}|[A-Za-z])\]")
_AUTHOR_YEAR = re.compile(
    r"\((?:[A-Z][\w'\N{RIGHT SINGLE QUOTATION MARK}-]+"
    r"(?:\s+(?:(?:and|&)\s+[A-Z][\w'\N{RIGHT SINGLE QUOTATION MARK}-]+|"
    r"et\s+al\.?))?),\s*(?:19|20)\d{2}[a-z]?\)"
)
_NAMED_ATTRIBUTION = re.compile(
    r"\b(?:according\s+to|as\s+(?:reported|found|described)\s+by)\b",
    re.IGNORECASE,
)
_ASCII_EMOTICON = re.compile(
    r"(?<!\w)(?:[:;=8xX]['\-^oO*]?[)(/\\DPpOo]|<3)(?!\w)"
)


class TargetMetricError(ValueError):
    """Raised when a target metric is ambiguous or unsafe to score."""


def normalize_text(text: str, *, case_sensitive: bool = False) -> str:
    value = unicodedata.normalize("NFKC", str(text))
    return value if case_sensitive else value.casefold()


def lexical_tokens(text: str, *, case_sensitive: bool = False) -> list[str]:
    """Return the exact lexical units used by all word-rate endpoints."""
    return _LEXICAL.findall(normalize_text(text, case_sensitive=case_sensitive))


# This intentionally uses a frozen, explicit table rather than Python's
# Unicode categories.  The latter do not expose the Unicode Emoji properties
# and can change with the Python runtime.  Ambiguous text symbols (for example
# U+00A9 COPYRIGHT SIGN) count only when followed by the emoji variation
# selector; default emoji-presentation characters count without one.
_DEFAULT_EMOJI_RANGES = (
    (0x231A, 0x231B),
    (0x23E9, 0x23EC),
    (0x23F0, 0x23F0),
    (0x23F3, 0x23F3),
    (0x25FD, 0x25FE),
    (0x2614, 0x2615),
    (0x2648, 0x2653),
    (0x267F, 0x267F),
    (0x2693, 0x2693),
    (0x26A1, 0x26A1),
    (0x26AA, 0x26AB),
    (0x26BD, 0x26BE),
    (0x26C4, 0x26C5),
    (0x26CE, 0x26CE),
    (0x26D4, 0x26D4),
    (0x26EA, 0x26EA),
    (0x26F2, 0x26F3),
    (0x26F5, 0x26F5),
    (0x26FA, 0x26FA),
    (0x26FD, 0x26FD),
    (0x2705, 0x2705),
    (0x270A, 0x270B),
    (0x2728, 0x2728),
    (0x274C, 0x274C),
    (0x274E, 0x274E),
    (0x2753, 0x2755),
    (0x2757, 0x2757),
    (0x2795, 0x2797),
    (0x27B0, 0x27B0),
    (0x27BF, 0x27BF),
    (0x2B1B, 0x2B1C),
    (0x2B50, 0x2B50),
    (0x2B55, 0x2B55),
    (0x1F000, 0x1F0FF),
    (0x1F18E, 0x1F18E),
    (0x1F191, 0x1F19A),
    (0x1F1E6, 0x1F1FF),
    (0x1F201, 0x1F201),
    (0x1F21A, 0x1F21A),
    (0x1F22F, 0x1F22F),
    (0x1F232, 0x1F23A),
    (0x1F250, 0x1F251),
    (0x1F300, 0x1FAFF),
)

_TEXT_EMOJI_RANGES = (
    (0x00A9, 0x00A9),
    (0x00AE, 0x00AE),
    (0x203C, 0x2049),
    (0x2122, 0x2139),
    (0x2194, 0x21FF),
    (0x2300, 0x23FF),
    (0x25A0, 0x25FF),
    (0x2600, 0x27BF),
    (0x2934, 0x2935),
    (0x2B00, 0x2BFF),
    (0x3030, 0x3030),
    (0x303D, 0x303D),
    (0x3297, 0x3297),
    (0x3299, 0x3299),
)

_EMOJI_VARIATION_SELECTOR = "\ufe0f"
_TEXT_VARIATION_SELECTOR = "\ufe0e"
_ZERO_WIDTH_JOINER = "\u200d"
_KEYCAP = "\u20e3"


def _in_codepoint_ranges(value: int, ranges: tuple[tuple[int, int], ...]) -> bool:
    return any(start <= value <= end for start, end in ranges)


def _is_regional_indicator(value: int) -> bool:
    return 0x1F1E6 <= value <= 0x1F1FF


def _is_emoji_modifier(value: int) -> bool:
    return 0x1F3FB <= value <= 0x1F3FF


def _is_emoji_component_start(text: str, index: int, *, joined: bool = False) -> bool:
    value = ord(text[index])
    if _is_emoji_modifier(value):
        return False
    if _in_codepoint_ranges(value, _DEFAULT_EMOJI_RANGES):
        return True
    has_emoji_selector = (
        index + 1 < len(text) and text[index + 1] == _EMOJI_VARIATION_SELECTOR
    )
    return _in_codepoint_ranges(value, _TEXT_EMOJI_RANGES) and (
        has_emoji_selector or joined
    )


def _consume_keycap(text: str, index: int) -> int | None:
    if text[index] not in "#*0123456789":
        return None
    end = index + 1
    if end < len(text) and text[end] in {
        _EMOJI_VARIATION_SELECTOR, _TEXT_VARIATION_SELECTOR,
    }:
        end += 1
    return end + 1 if end < len(text) and text[end] == _KEYCAP else None


def _consume_emoji_component(text: str, index: int) -> int:
    end = index + 1
    if end < len(text) and text[end] in {
        _EMOJI_VARIATION_SELECTOR, _TEXT_VARIATION_SELECTOR,
    }:
        end += 1
    if end < len(text) and _is_emoji_modifier(ord(text[end])):
        end += 1
    # Emoji subdivision flags are BLACK FLAG followed by tag characters and
    # CANCEL TAG.  Keeping the tags in the span prevents them being treated as
    # text between otherwise adjacent emoji clusters.
    while end < len(text) and 0xE0020 <= ord(text[end]) <= 0xE007E:
        end += 1
    if end < len(text) and ord(text[end]) == 0xE007F:
        end += 1
    return end


def emoji_cluster_spans(text: str) -> list[tuple[int, int]]:
    """Return spans for frozen, emoji-aware extended clusters.

    One flag pair, keycap sequence, skin-tone sequence, or complete ZWJ chain
    contributes one span.  Adjacent unjoined emoji remain separate clusters.
    Historical ``nla_lab.probes.emojis`` deliberately remains unchanged.
    """

    spans: list[tuple[int, int]] = []
    index = 0
    while index < len(text):
        keycap_end = _consume_keycap(text, index)
        if keycap_end is not None:
            spans.append((index, keycap_end))
            index = keycap_end
            continue

        value = ord(text[index])
        if _is_regional_indicator(value):
            end = index + 1
            if end < len(text) and _is_regional_indicator(ord(text[end])):
                end += 1
            spans.append((index, end))
            index = end
            continue

        if not _is_emoji_component_start(text, index):
            index += 1
            continue

        end = _consume_emoji_component(text, index)
        while end < len(text) and text[end] == _ZERO_WIDTH_JOINER:
            joined_index = end + 1
            if joined_index >= len(text) or not _is_emoji_component_start(
                text, joined_index, joined=True
            ):
                break
            end = _consume_emoji_component(text, joined_index)
        spans.append((index, end))
        index = end
    return spans


def emoji_cluster_metrics(text: str) -> dict[str, float | str]:
    """Return deterministic emoji efficacy and flood-monitoring metrics."""

    spans = emoji_cluster_spans(text)
    count = len(spans)
    # Keycap emoji contain an ASCII digit, which the general lexical tokenizer
    # correctly treats as a word/number. Remove complete emoji spans for this
    # target-specific denominator so ``1️⃣`` is not simultaneously one target
    # cluster and one lexical word.
    text_without_emoji: list[str] = []
    cursor = 0
    for start, end in spans:
        text_without_emoji.extend((text[cursor:start], " "))
        cursor = end
    text_without_emoji.append(text[cursor:])
    words = len(lexical_tokens("".join(text_without_emoji)))
    maximum_run = current_run = 0
    previous_end: int | None = None
    for start, end in spans:
        current_run = current_run + 1 if start == previous_end else 1
        maximum_run = max(maximum_run, current_run)
        previous_end = end
    word_denominator = max(words, 1)
    emoticon_count = len(_ASCII_EMOTICON.findall(unicodedata.normalize("NFKC", text)))
    return {
        "emoji_cluster_scorer": EMOJI_CLUSTER_SCORER_ID,
        "emoji_cluster_count": float(count),
        "emoji_cluster_present": float(count > 0),
        "emoji_rate_denominator_words": float(words),
        "emoji_clusters_per_100_words": float(100 * count / word_denominator),
        "emoji_cluster_to_word_fraction": float(count / word_denominator),
        "emoji_cluster_content_fraction": (
            float(count / (count + words)) if count + words else 0.0
        ),
        "max_adjacent_emoji_cluster_run": float(maximum_run),
        "ascii_emoticon_count": float(emoticon_count),
        "ascii_emoticon_present": float(emoticon_count > 0),
    }


def _max_run(items: list[str]) -> int:
    best = current = 0
    previous: str | None = None
    for item in items:
        if item == previous:
            current += 1
        else:
            previous, current = item, 1
        best = max(best, current)
    return best


def _max_character_run(text: str) -> int:
    chars = [c for c in unicodedata.normalize("NFKC", text) if not c.isspace()]
    return _max_run(chars)


def validate_target_metric(raw: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise TargetMetricError("target_metric must be a mapping")
    kind = str(raw.get("kind", ""))
    if kind not in {
        "word", "registered_probe", "citation_markers", "emoji_clusters",
    }:
        raise TargetMetricError(f"unsupported target metric kind {kind!r}")
    allowed = {
        "word": {"schema", "kind", "target", "case_sensitive", "minimum_presence_count"},
        "registered_probe": {"schema", "kind", "probe", "minimum_presence_count"},
        "citation_markers": {"schema", "kind", "minimum_presence_count"},
        "emoji_clusters": {"schema", "kind", "minimum_presence_count"},
    }[kind]
    extra = set(raw) - allowed
    if extra:
        raise TargetMetricError(f"unknown {kind} target metric fields: {sorted(extra)}")
    if raw.get("schema", SCHEMA) != SCHEMA:
        raise TargetMetricError(f"target_metric.schema must be {SCHEMA!r}")
    threshold = int(raw.get("minimum_presence_count", 1))
    if threshold < 1:
        raise TargetMetricError("minimum_presence_count must be >= 1")
    result: dict[str, Any] = {
        "schema": SCHEMA,
        "kind": kind,
        "minimum_presence_count": threshold,
    }
    if kind == "word":
        target = str(raw.get("target", ""))
        case_sensitive = bool(raw.get("case_sensitive", False))
        units = lexical_tokens(target, case_sensitive=case_sensitive)
        if len(units) != 1 or normalize_text(target, case_sensitive=case_sensitive) != units[0]:
            raise TargetMetricError(
                "word target must be exactly one lexical token under " + TOKENIZER_ID
            )
        result.update(target=units[0], case_sensitive=case_sensitive)
    elif kind == "registered_probe":
        probe = str(raw.get("probe", ""))
        if probe not in probes.ALL_PROBES:
            raise TargetMetricError(f"unknown registered probe {probe!r}")
        result["probe"] = probe
    return result


def _generic_citation_spans(text: str) -> list[tuple[int, int]]:
    normalized = unicodedata.normalize("NFKC", text)
    return [
        match.span()
        for pattern in (_URL, _BRACKET_REFERENCE, _AUTHOR_YEAR, _NAMED_ATTRIBUTION)
        for match in pattern.finditer(normalized)
    ]


def _supported_attribution_spans(
    text: str, prompt: Mapping[str, Any] | None
) -> list[tuple[int, int]]:
    if not prompt:
        return []
    allowed = prompt.get("allowed_attributions", [])
    if not isinstance(allowed, list):
        raise TargetMetricError("prompt allowed_attributions must be a list")
    normalized_labels = [normalize_text(str(label)).strip() for label in allowed]
    if any(not label for label in normalized_labels):
        raise TargetMetricError("allowed attribution labels must be non-empty")
    if len(set(normalized_labels)) != len(normalized_labels):
        raise TargetMetricError("allowed attribution labels must be unique")
    normalized_text = unicodedata.normalize("NFKC", text)
    spans: list[tuple[int, int]] = []
    for label in allowed:
        normalized_label = unicodedata.normalize("NFKC", str(label)).strip()
        pattern = re.compile(
            rf"(?<!\w){re.escape(normalized_label)}(?!\w)", re.IGNORECASE
        )
        spans.extend(match.span() for match in pattern.finditer(normalized_text))
    return spans


def _citation_event_count(
    text: str,
    generic_spans: list[tuple[int, int]],
    supported_spans: list[tuple[int, int]],
) -> int:
    """Count events, de-duplicating a generic marker tied to one source label."""
    normalized = unicodedata.normalize("NFKC", text)
    named = {match.span() for match in _NAMED_ATTRIBUTION.finditer(normalized)}
    paired_generic: set[int] = set()
    paired_supported: set[int] = set()
    for gi, (g_start, g_end) in enumerate(generic_spans):
        for si, (s_start, s_end) in enumerate(supported_spans):
            if gi in paired_generic or si in paired_supported:
                continue
            overlaps = max(g_start, s_start) < min(g_end, s_end)
            named_prefix = (
                (g_start, g_end) in named
                and s_start >= g_end
                and re.fullmatch(r"\s*", normalized[g_end:s_start]) is not None
            )
            if overlaps or named_prefix:
                paired_generic.add(gi)
                paired_supported.add(si)
    return len(generic_spans) + len(supported_spans) - len(paired_generic)


def coherence_metrics(text: str) -> dict[str, float]:
    tokens = lexical_tokens(text)
    counts = Counter(tokens)
    n = len(tokens)
    return {
        "unicode_word_count": float(n),
        "unique_unicode_word_ratio": float(len(counts) / n) if n else 0.0,
        "max_token_run": float(_max_run(tokens)),
        "dominant_token_fraction": float(max(counts.values()) / n) if n else 0.0,
        "max_character_run": float(_max_character_run(text)),
        "replacement_character_count": float(text.count("\N{REPLACEMENT CHARACTER}")),
    }


def score_target(
    text: str,
    raw_metric: Mapping[str, Any],
    *,
    prompt: Mapping[str, Any] | None = None,
) -> dict[str, float | str]:
    """Score the target plus a Unicode-aware degeneration battery."""
    metric = validate_target_metric(raw_metric)
    words = lexical_tokens(text)
    if metric["kind"] == "word":
        target = metric["target"]
        target_words = lexical_tokens(
            text, case_sensitive=bool(metric["case_sensitive"])
        )
        count = target_words.count(target)
    elif metric["kind"] == "registered_probe":
        count = int(probes.ALL_PROBES[metric["probe"]](text))
    elif metric["kind"] == "emoji_clusters":
        emoji_metrics = emoji_cluster_metrics(text)
        count = int(emoji_metrics["emoji_cluster_count"])
    else:
        generic_spans = _generic_citation_spans(text)
        supported_spans = _supported_attribution_spans(text, prompt)
        generic = len(generic_spans)
        supported = len(supported_spans)
        count = _citation_event_count(text, generic_spans, supported_spans)

    n_words = len(words)
    target_words = (
        int(emoji_metrics["emoji_rate_denominator_words"])
        if metric["kind"] == "emoji_clusters" else n_words
    )
    target_denominator = (
        max(target_words, 1) if metric["kind"] == "emoji_clusters" else target_words
    )
    result: dict[str, float | str] = {
        "metric_schema": SCHEMA,
        "lexical_tokenizer": TOKENIZER_ID,
        "target_count": float(count),
        "target_present": float(count >= metric["minimum_presence_count"]),
        "target_rate_per_100_words": (
            float(100 * count / target_denominator) if target_denominator else 0.0
        ),
        "target_token_fraction": (
            float(count / target_denominator) if target_denominator else 0.0
        ),
    }
    result.update(coherence_metrics(text))
    if metric["kind"] == "emoji_clusters":
        result.update(emoji_metrics)
    if metric["kind"] == "citation_markers":
        result["generic_citation_marker_count"] = float(generic)
        result["generic_citation_marker_rate_per_100_words"] = (
            float(100 * generic / n_words) if n_words else 0.0
        )
        result["supported_attribution_count"] = float(supported)
        result["supported_attribution_rate_per_100_words"] = (
            float(100 * supported / n_words) if n_words else 0.0
        )
    return result
