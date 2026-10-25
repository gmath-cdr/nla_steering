# Strict lexical scoring of the AXB44 blind-10 evaluation records, revised
# per the Codex QA entries [15] and [17] in NOTE_EXCHANGE.md.
#
# - Scores the exact sealed records in judge_evaluation.json, gated on the
#   pinned SHA-256 of alpha_receipt.json, COMPLETE.json, and
#   judge_evaluation.json for each run (fail closed).
# - Alpha selection is read from each run's sealed alpha_receipt.json
#   (chosen_alpha) and cross-checked against the paper-side analysis.json.
# - The strict matcher is the original `matches` boundary regex, vendored
#   verbatim from experiments/discovery500.py at source commit 3924034.
# - Writes a record-level artifact (no completion text) plus a provenance
#   block; all summaries are derived from that artifact.
# - Statistical reporting (predeclared before any outcome was viewed):
#   descriptive arm rates; paired mean differences with multilevel-bootstrap
#   95% CIs; the ONLY confirmatory p-values are the strict-metric family
#   {nla_vs_caa, nla_vs_diffmean}, Holm-adjusted (m=2), mirroring the sealed
#   judge analysis. All other contrasts are CI-only. The loose lexicon
#   variant is an unreported sensitivity check, CI-only.
#   `judge_score_by_strict_hit` is a conditional-mean summary over the five
#   non-random arms (400 rows, balanced union) plus a Pearson association;
#   it is not an agreement statistic.
#
# Modes: --selftest (matcher regression tests), --validate (structure +
# gates only, no outcomes), --full (blocked until the QA go).
import argparse, hashlib, json, os, re, sys
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent


def check(cond, msg):
    # validation-critical gate; unlike assert, survives python -O
    if not cond:
        raise SystemExit(f"GATE FAILED: {msg}")


DEF_QWEN = r"C:\Users\JPMorgan\Desktop\Algoverse\NLA Steering\.runpod_tmp\runpod-axb44-688f4c1-qwen-3i08n8e4yv926z\verified\qwen-run-02"
DEF_GEMMA = r"C:\Users\JPMorgan\Desktop\Algoverse\NLA Steering\.runpod_tmp\runpod-axb44-688f4c1-gemma-rdukyvm6ol1drr\download\verified-9f66522d67074f11a6279519ff8efcc1\gemma-run-02"
DEF_ANALYSIS = str((HERE.parent / "overleaf_v3" / "codex_axb44" / "analysis.json").resolve())
DEF_LAB = r"C:\Users\JPMorgan\Desktop\Algoverse\NLA Steering\nla-steering-effects\lab\experiments\axbench_lexicality20"

PINNED = {
    "qwen": {"alpha_receipt.json": "b074e0e5fe31a0b48cae98695ffc58c2812af54abf2983d924fac3d5268d78d8",
             "COMPLETE.json": "faa6d651ee5f3f3336bf90f47b72c34c4e435282a8706bdf58cf53210570519d",
             "judge_evaluation.json": "e471d274b5cf19c2e838858446c986d7ad32859239740ad7b4b1c141471d6d0e"},
    "gemma": {"alpha_receipt.json": "96a328b819c8f8f0ad5f0e3923368438b65c26b5cb9ef481c0a0f42da34f4004",
              "COMPLETE.json": "d251d0d70d7abcdbb9bb3292296db30f0c59afd4c73044b14ed7a08f812d4912",
              "judge_evaluation.json": "eebb6c0cd63e9790e38fd9953141badc0700cd7f4ce54885a7e17ee2651e1fc8"},
}
LEXICON_PAYLOAD_SHA = "6ce54f08a066281cf99b5df3d061bc254b3f27aa83ae682d5649c0ad18eb98c2"

METHODS = ("nla", "caa", "diffmean")
MODE_OF = {"nla": "last_and_decode", "caa": "all", "diffmean": "all"}
SEEDS = (17, 42, 101)
LOOSE_META = {"phrases", "words", "expressions", "descriptions"}
CONFIRMATORY = ("nla_vs_caa", "nla_vs_diffmean")  # strict metric only, Holm m=2
JUDGE_ASSOC_ARMS = ("nla", "caa", "diffmean", "unsteered", "prompt")
BOOTSTRAP_SEED = 20260827


# vendored verbatim from experiments/discovery500.py @ commit
# 392403413c8580f1ccc5cf260fc04c4dfed40b62 (lines 57-62)
def matches(text, terms):
    if not terms:
        return []
    pattern = re.compile(r"(?i)(?<!\w)(?:" + "|".join(
        re.escape(term) for term in sorted(terms, key=len, reverse=True)) + r")(?!\w)")
    return pattern.findall(text)


def stem(w):
    for suf in ("ing", "ed", "es", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 4:
            return w[: len(w) - len(suf)]
    return w


def loose_matches(text, terms):
    # sensitivity variant only: drop meta-words, allow simple suffix variants
    kept = [t for t in terms if t not in LOOSE_META]
    if not kept:
        return []
    alts = sorted({stem(t) for t in kept}, key=len, reverse=True)
    pattern = re.compile(r"(?i)(?<!\w)(?:" + "|".join(
        re.escape(b) + r"(?:s|es|ed|ing)?" for b in alts) + r")(?!\w)")
    return pattern.findall(text)


def sha256_file(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def gate(root, mk):
    for fname, want in PINNED[mk].items():
        got = sha256_file(os.path.join(root, fname))
        if got != want:
            sys.exit(f"HASH GATE FAILED {mk}/{fname}: got {got}, pinned {want}")
    return {f: PINNED[mk][f] for f in PINNED[mk]}


def load_model(root, mk, analysis):
    gate(root, mk)
    receipt = json.load(open(os.path.join(root, "alpha_receipt.json"), encoding="utf-8"))
    status = receipt.get("status")
    check(status == "sealed", f"receipt status {status!r} != sealed")
    sel = {cid: dict(m) for cid, m in receipt["chosen_alpha"].items()}
    paper_sel = analysis["models"][mk]["selected_calibration_alpha"]
    if sel != paper_sel:
        sys.exit(f"ALPHA MISMATCH {mk}: receipt chosen_alpha != analysis.json selected_calibration_alpha")
    rows = list(json.load(open(os.path.join(root, "judge_evaluation.json"),
                               encoding="utf-8"))["records"].values())
    return rows, sel


def pair_key(row):
    return int(row["concept_id"]), str(row["prompt_id"]), int(row["generation_seed"])


def method_rows(rows, sel, method):
    return [r for r in rows if r["arm_id"] == method
            and abs(float(r["alpha"]) - float(sel[str(r["concept_id"])][method])) < 1e-9]


def matched_random(rows, seed):
    idx = {}
    prefix = f"random_{seed}_"
    for r in rows:
        if not str(r["arm_id"]).startswith(prefix):
            continue
        mode = "last_and_decode" if "_last_and_decode_" in r["arm_id"] else "all"
        k = (*pair_key(r), mode, round(float(r["alpha"]), 10))
        check(k not in idx, f"duplicate random cell {k}")
        idx[k] = r
    return idx


def build_arms(rows, sel):
    arms = {m: method_rows(rows, sel, m) for m in METHODS}
    arms["unsteered"] = [r for r in rows if r["arm_id"] == "unsteered"]
    arms["prompt"] = [r for r in rows if r["arm_id"] == "axbench_prompt_steering"]
    for m in METHODS:
        for seed in SEEDS:
            idx = matched_random(rows, seed)
            arms[f"random_{seed}_matched_{m}"] = [
                idx[(*pair_key(r), MODE_OF[m], round(float(r["alpha"]), 10))] for r in arms[m]]
    return arms


def selftest():
    lex = ["estate", "ownership", "property", "real"]
    t = lambda s, terms=lex: bool(matches(s, terms))
    checks = [
        (t("I love Real estate."), True, "case-insensitive"),
        (t("really unreal surreal"), False, "substring must not match"),
        (t("real-world example"), True, "hyphen neighbor counts (real in real-world)"),
        (t("(property!)"), True, "punctuation boundaries"),
        (t("proper tea"), False, "no partial word"),
        (bool(matches("time-related data", ["time-related"])), True, "hyphenated lexicon term"),
        (bool(matches("real estate boom", ["real estate"])), True, "multiword lexicon term"),
        (bool(matches("surreal estates", ["real"])), False, "suffix context still bounded"),
        (bool(loose_matches("many properties sold", ["property"])), False,
         "loose stems property->propert, properties->properti (documented: crude stemmer)"),
        (bool(loose_matches("discounts everywhere", ["discount"])), True, "loose plural"),
        (bool(loose_matches("nice phrases here", ["phrases", "elegance"])), False, "loose drops meta-words"),
        (matches("Real estate, real ESTATE", lex) == ["Real", "estate", "real", "ESTATE"], True,
         "findall returns each occurrence"),
    ]
    bad = [(msg, got, want) for got, want, msg in checks if got != want]
    for msg, got, want in bad:
        print(f"SELFTEST FAIL: {msg}: got {got}, want {want}")
    if bad:
        sys.exit(1)
    print(f"SELFTEST PASSED ({len(checks)} checks)")


def validate(cfg):
    analysis = json.load(open(cfg.analysis, encoding="utf-8"))
    for mk, root in (("qwen", cfg.qwen_root), ("gemma", cfg.gemma_root)):
        rows, sel = load_model(root, mk, analysis)
        print(f"== {mk}: {len(rows)} records; hash gate + sealed receipt OK; receipt==analysis alphas ==")
        arms = build_arms(rows, sel)
        for m in METHODS:
            check(len(arms[m]) == 80, f"{m}: {len(arms[m])} rows != 80")
        check(len(arms["unsteered"]) == 80 and len(arms["prompt"]) == 80,
              "unsteered/prompt cardinality != 80")
        base = {pair_key(r) for r in arms["unsteered"]}
        for name in ("nla", "caa", "diffmean", "prompt"):
            n = sum(1 for r in arms[name] if pair_key(r) in base)
            check(n == 80, f"{name} pairs with unsteered: {n} != 80")
        print("  80 rows/arm at receipt alphas; randoms matched; all arms pair 80/80 with unsteered")
        lex = json.load(open(cfg.lexicons, encoding="utf-8"))["lexicons"]
        cids = sorted({str(r["concept_id"]) for r in rows})
        check(all(lex.get(c) for c in cids), f"missing/empty lexicons for {cids}")
    canon = json.dumps(json.load(open(cfg.lexicons, encoding="utf-8")), indent=1, sort_keys=True)
    got = hashlib.sha256(canon.encode()).hexdigest()
    check(got == LEXICON_PAYLOAD_SHA, f"lexicon payload hash {got} != frozen {LEXICON_PAYLOAD_SHA}")
    print("  lexicon payload hash matches frozen receipt")
    print("VALIDATION PASSED (no outcomes computed)")


def full(cfg):
    sys.path.insert(0, cfg.lab)
    from analyze_results import multilevel_bootstrap
    analysis = json.load(open(cfg.analysis, encoding="utf-8"))
    lex_doc = json.load(open(cfg.lexicons, encoding="utf-8"))
    canon = json.dumps(lex_doc, indent=1, sort_keys=True)
    check(hashlib.sha256(canon.encode()).hexdigest() == LEXICON_PAYLOAD_SHA,
          "lexicon payload hash != frozen receipt")
    lex = lex_doc["lexicons"]

    provenance = {
        "pinned_run_hashes": PINNED,
        "lexicon_payload_sha256_lf_utf8": LEXICON_PAYLOAD_SHA,
        "scorer_sha256": sha256_file(os.path.abspath(__file__)),
        "analyze_results_sha256": sha256_file(os.path.join(cfg.lab, "analyze_results.py")),
        "analysis_json_sha256": sha256_file(cfg.analysis),
        "panel14_snapshot_sha256": sha256_file(str(HERE / "panel14_source_snapshot.json")),
        "matcher": "matches() vendored verbatim from experiments/discovery500.py@3924034",
        "bootstrap_seed": BOOTSTRAP_SEED,
        "confirmatory_family": {"metric": "strict", "contrasts": list(CONFIRMATORY),
                                "adjustment": "holm", "m": len(CONFIRMATORY)},
        "judge_assoc_scope": f"balanced_union rows of arms {JUDGE_ASSOC_ARMS} (400/model)",
        "loose_variant": "sensitivity only, unreported; drops " + ",".join(sorted(LOOSE_META))
                         + "; suffix-tolerant stems",
    }

    # --- record-level artifact (no completion text) ---
    os.makedirs(cfg.out_dir, exist_ok=True)
    rec_path = os.path.join(cfg.out_dir, "strict_blind10_records.jsonl")
    all_arms = {}
    with open(rec_path, "w", encoding="utf-8", newline="\n") as out:
        for mk, root in (("qwen", cfg.qwen_root), ("gemma", cfg.gemma_root)):
            rows, sel = load_model(root, mk, analysis)
            arms = build_arms(rows, sel)
            all_arms[mk] = arms
            seen = set()
            for arm_name, rs in arms.items():
                for r in rs:
                    terms = lex[str(r["concept_id"])]
                    ms = matches(r["sentence"], terms)
                    ml = loose_matches(r["sentence"], terms)
                    r.setdefault("_roles", []).append(arm_name)
                    r["_hs"], r["_hl"] = int(bool(ms)), int(bool(ml))
                    if r["record_key"] in seen:
                        continue  # one line per record; roles listed
                    seen.add(r["record_key"])
                    out.write(json.dumps({
                        "model": mk, "record_key": r["record_key"],
                        "arm_id": r["arm_id"], "alpha": r["alpha"],
                        "mode": ("last_and_decode" if "_last_and_decode_" in r["arm_id"]
                                 else ("all" if r["arm_id"].startswith("random_") else MODE_OF.get(r["arm_id"], "n/a"))),
                        "concept_id": r["concept_id"], "prompt_id": r["prompt_id"],
                        "prompt_track": r["prompt_track"],
                        "generation_seed": r["generation_seed"],
                        "strict_hit": int(bool(ms)),
                        "matched_terms": sorted({m.lower() for m in ms}),
                        "loose_hit": int(bool(ml)),
                        "axbench_harmonic": r["axbench_harmonic"],
                    }) + "\n")

    # --- artifact verification, then summaries derived from it ---
    expected = sum(len({r["record_key"] for rs in all_arms[mk].values() for r in rs})
                   for mk in all_arms)
    recs = [json.loads(line) for line in open(rec_path, encoding="utf-8")]
    ids = [(r["model"], r["record_key"]) for r in recs]
    check(len(ids) == len(set(ids)), "duplicate (model, record_key) in record artifact")
    check(len(recs) == expected, f"record artifact has {len(recs)} rows, expected {expected}")
    provenance["record_artifact"] = {"rows": len(recs), "sha256": sha256_file(rec_path)}
    by_key = {(r["model"], r["record_key"]): r for r in recs}
    def R(mk, row):
        return by_key[(mk, row["record_key"])]

    def rate(mk, rs, f):
        sub = {"balanced_union": rs,
               "concept500_negative": [r for r in rs if r["prompt_track"] == "concept500_negative"],
               "shared_alpaca_transfer": [r for r in rs if r["prompt_track"] == "shared_alpaca_transfer"]}
        return {t: {"rate": round(sum(R(mk, r)[f] for r in v) / len(v), 4), "n": len(v)}
                for t, v in sub.items()}

    def holm(ps):
        order = sorted(range(len(ps)), key=lambda i: ps[i])
        adj, run = [0.0] * len(ps), 0.0
        for rank, i in enumerate(order):
            run = max(run, (len(ps) - rank) * ps[i])
            adj[i] = min(1.0, run)
        return adj

    report = {"provenance": provenance, "models": {}}
    for mk in ("qwen", "gemma"):
        arms = all_arms[mk]
        M = {"arm_rates": {}, "contrasts": {}}
        for name, rs in arms.items():
            M["arm_rates"][name] = {"strict": rate(mk, rs, "strict_hit"),
                                    "loose": rate(mk, rs, "loose_hit")}

        keyed = {n: {pair_key(r): r for r in arms[n]} for n in arms}
        def contrast(left, right, f, confirmatory):
            paired = []
            for r in arms[left]:
                o = keyed[right].get(pair_key(r))
                check(o is not None, f"unpaired row {left} vs {right}: {pair_key(r)}")
                paired.append({"concept_id": r["concept_id"], "prompt_id": r["prompt_id"],
                               "generation_seed": r["generation_seed"],
                               "difference": float(R(mk, r)[f]) - float(R(mk, o)[f])})
            check(len(paired) == 80, f"{left} vs {right}: {len(paired)} pairs != 80")
            b = multilevel_bootstrap(paired, seed=BOOTSTRAP_SEED)
            res = {"n_pairs": 80,
                   "mean_difference": round(sum(p["difference"] for p in paired) / 80, 4),
                   "ci95_low": b["ci95_low"], "ci95_high": b["ci95_high"]}
            if confirmatory:
                res["bootstrap_p_two_sided_unadjusted"] = b["bootstrap_p_two_sided"]
            return res

        plan = [("nla_vs_caa", "nla", "caa"), ("nla_vs_diffmean", "nla", "diffmean")]
        plan += [(f"{n}_vs_unsteered", n, "unsteered") for n in ("nla", "caa", "diffmean", "prompt")]
        plan += [(f"{m}_vs_random_{s}", m, f"random_{s}_matched_{m}") for m in METHODS for s in SEEDS]
        for f, tag in (("strict_hit", "strict"), ("loose_hit", "loose")):
            C = {}
            for cname, l, rgt in plan:
                conf = tag == "strict" and cname in CONFIRMATORY
                C[cname] = contrast(l, rgt, f, conf)
            if tag == "strict":
                ps = [C[c]["bootstrap_p_two_sided_unadjusted"] for c in CONFIRMATORY]
                for c, p in zip(CONFIRMATORY, holm(ps)):
                    C[c]["holm_adjusted_p"] = round(p, 6)
            M["contrasts"][tag] = C

        scope = [R(mk, r) for n in JUDGE_ASSOC_ARMS for r in arms[n]]
        h1 = [r["axbench_harmonic"] for r in scope if r["strict_hit"] == 1]
        h0 = [r["axbench_harmonic"] for r in scope if r["strict_hit"] == 0]
        n = len(scope)
        mx = sum(r["strict_hit"] for r in scope) / n
        my = sum(r["axbench_harmonic"] for r in scope) / n
        cov = sum((r["strict_hit"] - mx) * (r["axbench_harmonic"] - my) for r in scope) / n
        sx = (sum((r["strict_hit"] - mx) ** 2 for r in scope) / n) ** 0.5
        sy = (sum((r["axbench_harmonic"] - my) ** 2 for r in scope) / n) ** 0.5
        M["judge_score_by_strict_hit"] = {
            "scope": provenance["judge_assoc_scope"], "n": n,
            "mean_judge_harmonic_hit": round(sum(h1) / len(h1), 4) if h1 else None,
            "n_hit": len(h1),
            "mean_judge_harmonic_miss": round(sum(h0) / len(h0), 4) if h0 else None,
            "pearson_r": round(cov / (sx * sy), 4) if sx * sy > 0 else None,
        }
        report["models"][mk] = M

    rp = os.path.join(cfg.out_dir, "strict_blind10.json")
    open(rp, "w", encoding="utf-8", newline="\n").write(json.dumps(report, indent=1))
    for mk, M in report["models"].items():
        print(f"\n==== {mk} ====")
        for tag in ("strict", "loose"):
            print(f"-- {tag}: rates balanced|neg|alpaca --")
            for name, v in M["arm_rates"].items():
                b, ng, al = (v[tag][t]["rate"] for t in
                             ("balanced_union", "concept500_negative", "shared_alpaca_transfer"))
                print(f"  {name:<26} {b:.3f} | {ng:.3f} | {al:.3f}")
            for cname, c in M["contrasts"][tag].items():
                extra = ""
                if "holm_adjusted_p" in c:
                    extra = f" p_holm={c['holm_adjusted_p']:.4f}"
                print(f"  {cname:<26} diff={c['mean_difference']:+.3f} "
                      f"ci=[{c['ci95_low']:+.3f},{c['ci95_high']:+.3f}]{extra}")
        j = M["judge_score_by_strict_hit"]
        print(f"  judge harmonic by strict hit: hit {j['mean_judge_harmonic_hit']} (n={j['n_hit']}) "
              f"miss {j['mean_judge_harmonic_miss']} | pearson r={j['pearson_r']}")
    print(f"\nwrote {rec_path}\nwrote {rp}")


ap = argparse.ArgumentParser()
ap.add_argument("--qwen-root", default=DEF_QWEN)
ap.add_argument("--gemma-root", default=DEF_GEMMA)
ap.add_argument("--analysis", default=DEF_ANALYSIS)
ap.add_argument("--lexicons", default=str(HERE / "blind10_lexicons_v2.json"))
ap.add_argument("--lab", default=DEF_LAB)
ap.add_argument("--out-dir", default=str(HERE))
ap.add_argument("--selftest", action="store_true")
ap.add_argument("--validate", action="store_true")
ap.add_argument("--full", action="store_true")
cfg = ap.parse_args()
if cfg.selftest:
    selftest()
elif cfg.validate:
    validate(cfg)
elif cfg.full:
    full(cfg)
else:
    sys.exit("pass --selftest, --validate, or --full")
