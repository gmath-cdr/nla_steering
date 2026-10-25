"""Deterministic behavior probes: judge-free scoring of generated text.

Each probe is a plain count/ratio on the output string, so steering effects
are measurable without a judge model. These mirror the probe list in the
research doc (ellipses, emoji, lists, contractions, ...). Near-zero-baseline
probes (emoji, ellipsis) give the cleanest signal: any increase is real.
"""

from __future__ import annotations

import re
import statistics

_EMOJI = re.compile(
    "[\U0001f300-\U0001faff\U0001f000-\U0001f0ff☀-➿⬀-⯿]"
)
_LIST_MARKER = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+", re.MULTILINE)
_CONTRACTION = re.compile(
    r"\b(?:don't|doesn't|didn't|can't|won't|isn't|aren't|wasn't|weren't|"
    r"I'm|I've|I'll|it's|that's|you're|you've|we're|they're|let's)\b",
    re.IGNORECASE,
)
_FIRST_PERSON = re.compile(r"\b(?:I|me|my|mine|myself)\b")
_SECOND_PERSON = re.compile(r"\b(?:you|your|yours|yourself)\b", re.IGNORECASE)
_PASSIVE = re.compile(r"\b(?:was|were|is|are|been|being)\s+\w+ed\b", re.IGNORECASE)
_ARCHAIC = re.compile(
    r"\b(?:thee|thou|thy|thine|hath|doth|dost|shalt|art|wherefore|verily|"
    r"ere|o'er|'tis|forsooth|prithee)\b",
    re.IGNORECASE,
)
_HEDGE = re.compile(
    r"\b(?:might|maybe|perhaps|possibly|probably|likely|seems?|appears?|"
    r"arguably|somewhat|generally|typically|often|usually|in some cases|"
    r"it depends|not necessarily|to some extent)\b",
    re.IGNORECASE,
)
_PARENTHETICAL_SPAN = re.compile(r"\(([^()\r\n]+)\)")
_AUTHOR_YEAR_INNER = re.compile(
    r"\s*[A-Z][A-Za-z'\N{RIGHT SINGLE QUOTATION MARK}-]+"
    r"(?:\s+(?:(?:and|&)\s+"
    r"[A-Z][A-Za-z'\N{RIGHT SINGLE QUOTATION MARK}-]+|et\s+al\.?))?,"
    r"\s*(?:19|20)\d{2}[a-z]?\s*"
)


def ellipses(text: str) -> int:
    return text.count("...") + text.count("…")


def emojis(text: str) -> int:
    return len(_EMOJI.findall(text))


def em_dashes(text: str) -> int:
    return text.count("—")


def exclamations(text: str) -> int:
    return text.count("!")


def list_markers(text: str) -> int:
    return len(_LIST_MARKER.findall(text))


def ends_with_question(text: str) -> int:
    return int(text.rstrip().endswith("?"))


def contractions(text: str) -> int:
    return len(_CONTRACTION.findall(text))


def first_person(text: str) -> int:
    return len(_FIRST_PERSON.findall(text))


def second_person(text: str) -> int:
    return len(_SECOND_PERSON.findall(text))


def digits(text: str) -> int:
    return sum(c.isdigit() for c in text)


def parentheticals(text: str) -> int:
    return text.count("(")


def balanced_parentheticals(text: str) -> int:
    """Balanced, non-empty parenthetical spans, excluding author-year cites."""
    return sum(
        1 for match in _PARENTHETICAL_SPAN.finditer(text)
        if not _AUTHOR_YEAR_INNER.fullmatch(match.group(1))
    )


def passive_markers(text: str) -> int:
    return len(_PASSIVE.findall(text))


def caps_words(text: str) -> int:
    """Words of 3+ letters written in ALL CAPS (emphasis / shouting)."""
    return sum(1 for w in text.split() if len(w) >= 3 and w.isalpha() and w.isupper())


def bold_pairs(text: str) -> int:
    """Markdown bold spans (** pairs). Qwen's baseline here is NOT near zero."""
    return text.count("**") // 2


def hedge_words(text: str) -> int:
    """Epistemic hedging markers, the judge-free hedging score."""
    return len(_HEDGE.findall(text))


def archaic_words(text: str) -> int:
    """Shakespeare-style vocabulary. Near-zero baseline in normal replies,
    so it deterministically replicates the team's LLM-judged concept score."""
    return len(_ARCHAIC.findall(text))


_WORDS = re.compile(r"[A-Za-z']+")
_TITLECASE = re.compile(r"\b[A-Z][a-z]+\b")


def alliteration_pairs(text: str) -> int:
    """Adjacent word pairs sharing a first letter (research doc probe 1).

    Chance baseline in ordinary English is well above zero, so read this
    against the census distribution, not against zero.
    """
    words = [w.lower() for w in _WORDS.findall(text)]
    return sum(1 for a, b in zip(words, words[1:]) if a[0] == b[0])


def midsentence_caps(text: str) -> int:
    """Title-Case words not at a sentence start (noun-capitalization proxy).

    Counts Capitalized words rather than verified nouns, so it stays
    deterministic without a POS tagger; unsteered baseline should be
    proper nouns only.
    """
    count = 0
    for m in _TITLECASE.finditer(text):
        head = text[: m.start()].rstrip()
        if not head or head[-1] in ".!?:;\"'“”‘’()*-•#>":
            continue
        count += 1
    return count


_ALPHA_TOK = re.compile(r"[A-Za-z]+")


_QUOTED = re.compile(r"[\"'“‘]([A-Za-z]{5})[.,!?]?[\"'”’]")
_SPELLED = re.compile(r"\b([A-Za-z])\s*-\s*([A-Za-z])\s*-\s*([A-Za-z])"
                      r"\s*-\s*([A-Za-z])\s*-\s*([A-Za-z])\b")


def wordle_answer(text: str) -> str:
    """Deterministic answer extraction for bare-prompt Wordle replies,
    in preference order: a QUOTED 5-letter word (models quote their
    answer: a word you could use is "crane"), else an ALL-CAPS 5-letter
    token, else the LAST 5-letter word (the answer of "a word you could
    use is crane"; first-word rules get robbed by auxiliaries like
    'could'), else empty."""
    sp = _SPELLED.search(text)
    if sp:
        return "".join(sp.groups()).lower()
    m = _QUOTED.search(text)
    if m:
        return m.group(1).lower()
    toks = _ALPHA_TOK.findall(text)
    for t in toks:
        if len(t) == 5 and t.isupper():
            return t.lower()
    for t in reversed(toks):
        if len(t) == 5:
            return t.lower()
    return ""


def wordle_valid(text: str) -> int:
    """A 5-letter answer is extractable at all: the coherence probe for
    Wordle runs. If steering breaks this, the answer word is moot."""
    return int(wordle_answer(text) != "")


def wordle_crane(text: str) -> int:
    """Extracted answer is CRANE, the census modal opener (25/25 under
    the formatted prompt). The wordle-away target probe."""
    return int(wordle_answer(text) == "crane")


_QUOTED_ANY = re.compile(r"[\"'“‘]([A-Za-z]{3,9})[.,!?]?[\"'”’]")
_SPELLED_ANY = re.compile(r"\b[A-Za-z](?:\s*-\s*[A-Za-z]){2,8}\b")


def wordle_answer_any(text: str) -> str:
    """Length-agnostic answer extraction (3-9 letters), same preference
    chain as wordle_answer: spelled, quoted, ALL-CAPS token, last
    in-range word. Exists because the 5-only extractor is blind by
    construction to the length-steering arms' success condition."""
    sp = _SPELLED_ANY.search(text)
    if sp:
        return re.sub(r"[\s-]+", "", sp.group(0)).lower()
    m = _QUOTED_ANY.search(text)
    if m:
        return m.group(1).lower()
    toks = _ALPHA_TOK.findall(text)
    for t in toks:
        if 3 <= len(t) <= 9 and t.isupper():
            return t.lower()
    for t in reversed(toks):
        if 3 <= len(t) <= 9:
            return t.lower()
    return ""


def answer_letters(text: str) -> int:
    """Letter count of the extracted answer, 0 if none extractable.
    The length-dial target probe."""
    return len(wordle_answer_any(text))


def wordle_any_valid(text: str) -> int:
    """An answer of ANY length (3-9) is extractable."""
    return int(wordle_answer_any(text) != "")


def n_words(text: str) -> int:
    return len(text.split())


def avg_sentence_words(text: str) -> float:
    sents = [s for s in re.split(r"[.!?]+", text) if s.strip()]
    if not sents:
        return 0.0
    return statistics.mean(len(s.split()) for s in sents)


def unique_word_ratio(text: str) -> float:
    """Distinct whitespace tokens / total tokens: the collapse detector.

    Degenerate repetition scores near 0 while n_words stays high, so this
    is the coherence probe to plot next to every target probe. Counts
    whitespace tokens, NOT alphabetic words: wave-0 data showed "* * * *"
    collapse reading as HIGH diversity under a words-only count (asterisks
    are not words). Normal prose sits around 0.6-0.75.
    """
    toks = [t.lower() for t in text.split()]
    if not toks:
        return 0.0
    return len(set(toks)) / len(toks)


ALL_PROBES = {
    "ellipses": ellipses,
    "emojis": emojis,
    "em_dashes": em_dashes,
    "exclamations": exclamations,
    "list_markers": list_markers,
    "ends_with_question": ends_with_question,
    "contractions": contractions,
    "first_person": first_person,
    "second_person": second_person,
    "digits": digits,
    "parentheticals": parentheticals,
    "balanced_parentheticals": balanced_parentheticals,
    "passive_markers": passive_markers,
    "archaic_words": archaic_words,
    "caps_words": caps_words,
    "bold_pairs": bold_pairs,
    "hedge_words": hedge_words,
    "alliteration_pairs": alliteration_pairs,
    "midsentence_caps": midsentence_caps,
    "wordle_valid": wordle_valid,
    "wordle_crane": wordle_crane,
    # wordle_answer_any is string-valued: importable for analysis, but
    # NOT registered (the runner's summary averages probe values)
    "answer_letters": answer_letters,
    "wordle_any_valid": wordle_any_valid,
    "n_words": n_words,
    "avg_sentence_words": avg_sentence_words,
    "unique_word_ratio": unique_word_ratio,
}


def score_all(text: str) -> dict[str, float]:
    return {name: fn(text) for name, fn in ALL_PROBES.items()}


def score_table(outputs: dict[float, str], probe_names: list[str] | None = None) -> str:
    """Format {alpha: text} as a probe table, one row per alpha."""
    names = probe_names or list(ALL_PROBES)
    header = "alpha   " + "  ".join(f"{n:>18}" for n in names)
    lines = [header, "-" * len(header)]
    for alpha, text in outputs.items():
        scores = score_all(text)
        row = f"{alpha:<7g} " + "  ".join(f"{scores[n]:>18.1f}" for n in names)
        lines.append(row)
    return "\n".join(lines)
