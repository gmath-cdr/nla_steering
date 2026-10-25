"""Freeze the J-lens fitting corpus. Laptop, no GPU, stdlib only.

    python experiments/jspace/fetch_prompts.py

Replicates jlens.examples.load_wikitext_prompts (the corpus loader shipped
with anthropics/jacobian-lens): the first N_PROMPTS WikiText-103 train
records whose stripped text is at least MIN_CHARS characters, in dataset
order. Deterministic, so rerunning reproduces the same file. Fetches via
the HuggingFace datasets-server rows API instead of the datasets library
so the laptop needs no ML stack. Writes data/fit_prompts.json.
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

N_PROMPTS = 160
MIN_CHARS = 600
PAGE = 100  # rows API maximum page size
API = "https://datasets-server.huggingface.co/rows"
DATASET = {"dataset": "Salesforce/wikitext",
           "config": "wikitext-103-raw-v1", "split": "train"}


def fetch_page(offset: int) -> list[dict]:
    query = urllib.parse.urlencode({**DATASET, "offset": offset, "length": PAGE})
    req = urllib.request.Request(f"{API}?{query}",
                                 headers={"User-Agent": "nla-steering-lab"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))["rows"]
        except Exception as exc:  # noqa: BLE001 - retry then re-raise
            if attempt == 3:
                raise
            print(f"  page offset={offset} failed ({exc}), retrying")
            time.sleep(2 * (attempt + 1))
    raise AssertionError("unreachable")


def main() -> None:
    out_dir = Path(__file__).resolve().parent / "data"
    out_dir.mkdir(exist_ok=True)
    prompts: list[dict] = []
    offset = 0
    while len(prompts) < N_PROMPTS:
        for row in fetch_page(offset):
            text = row["row"]["text"]
            if len(text.strip()) >= MIN_CHARS:
                prompts.append({"row_idx": row["row_idx"], "text": text})
                if len(prompts) == N_PROMPTS:
                    break
        offset += PAGE
        print(f"scanned {offset} rows, kept {len(prompts)}/{N_PROMPTS}")

    lengths = sorted(len(p["text"]) for p in prompts)
    report = {
        "config": {
            "source": f"{DATASET['dataset']} / {DATASET['config']} / "
                      f"{DATASET['split']} via datasets-server rows API",
            "recipe": "first N records with len(text.strip()) >= MIN_CHARS, "
                      "dataset order; replicates "
                      "jlens.examples.load_wikitext_prompts",
            "n_prompts": N_PROMPTS, "min_chars": MIN_CHARS,
            "split_half": "A = prompts[:80], B = prompts[80:]",
            "char_len_min_median_max": [lengths[0],
                                        lengths[len(lengths) // 2],
                                        lengths[-1]],
        },
        "prompts": prompts,
    }
    out_path = out_dir / "fit_prompts.json"
    out_path.write_text(json.dumps(report, indent=1, ensure_ascii=False),
                        encoding="utf-8")
    print(f"saved {out_path} ({out_path.stat().st_size // 1024} KB, "
          f"char lengths min/median/max = {lengths[0]}/"
          f"{lengths[len(lengths) // 2]}/{lengths[-1]})")


if __name__ == "__main__":
    main()
