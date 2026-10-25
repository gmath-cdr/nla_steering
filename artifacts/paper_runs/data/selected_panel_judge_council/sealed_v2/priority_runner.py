# Transport-ordering wrapper per Codex entry [48]: schedule the four
# paper-grid common arms (unsteered, caa_calibrated, diffmean, nla_av_edit)
# ahead of the fixed-CAA and random arms. This changes NOTHING scientific:
# the original 3,584-record manifest and 10,752-call expected keyset stay
# sealed; keys are selected only from the sealed all_call_keys() map; model,
# templates, parser, request body, checkpoint, and retry/429 machinery are
# the sealed runner's own (loaded by exec, file untouched). A receipt records
# this driver's hash and the before/after checkpoint hashes per pass.
# Completion of the four arms is NOT completion of the full judge.
#
# Modes:
#   --run        one pass over pending common-arm keys
#                (exit 0 = common arms complete; 2 = still pending)
#   --summarize  fail-closed summary of the four common arms only
#                (hardened per-call checks; labeled partial)
import argparse, hashlib, json, os, sys, time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

HERE = Path(__file__).resolve().parent
COMMON_ARMS = ("unsteered", "caa_calibrated", "diffmean", "nla_av_edit")

_ns = {"__file__": str(HERE / "judge_panel14_v2.py"), "__name__": "judge_panel14_v2_ns"}
_argv = sys.argv
sys.argv = ["judge_panel14_v2.py"]
try:
    exec(compile(open(HERE / "judge_panel14_v2.py", encoding="utf-8").read(),
                 str(HERE / "judge_panel14_v2.py"), "exec"), _ns)
except SystemExit:
    pass
finally:
    sys.argv = _argv
G = _ns


def common_keymap(judge, items):
    keymap = G["all_call_keys"](judge, items)
    return {k: v for k, v in keymap.items() if v[0]["arm_id"] in COMMON_ARMS}


def run(judge, out_dir, workers):
    spec = G["JUDGES"][judge]
    api_key = os.environ.get(spec["env"], "")
    if not api_key:
        sys.exit(f"{spec['env']} is not set")
    items = G["load_items"]()
    G["verify_manifest"](items)
    out, ck = G["open_checkpoint"](judge, out_dir)
    before = G["file_sha256"](out) if out.exists() else None
    km = common_keymap(judge, items)
    pending = [(k, v[2]) for k, v in sorted(km.items()) if k not in ck["calls"]]
    print(f"[priority] common-arm calls: {len(km)} | cached: {len(km)-len(pending)} "
          f"| pending this pass: {len(pending)}", flush=True)
    calls, errors = ck["calls"], ck["errors"]
    G["ABORT"].clear()
    G["_429_STREAK"][0] = 0
    done = 0
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = {pool.submit(G["call_with_retry"], judge, prompt, api_key, key, errors): key
                   for key, prompt in pending}
        pending_prompts = dict(pending)
        saved = 0
        cancelled = False
        for future in as_completed(futures):
            if G["ABORT"].is_set() and not cancelled:
                cancelled = True
                for f in futures:
                    f.cancel()
            if future.cancelled():
                continue
            try:
                key, response = future.result()
            except Exception as exc:
                if not G["ABORT"].is_set() or "aborted" not in str(exc):
                    print(f"[priority] PENDING-KEPT: {exc}", flush=True)
                continue
            response["request_prompt_sha256"] = hashlib.sha256(
                pending_prompts[key].encode("utf-8")).hexdigest()
            calls[key] = response
            done += 1
            saved += 1
            if saved >= 50:
                G["atomic_json"](out, ck)
                saved = 0
                print(f"[priority] {done}/{len(pending)} new", flush=True)
    finally:
        pool.shutdown(wait=True)
        G["atomic_json"](out, ck)
    after = G["file_sha256"](out)
    remaining = [k for k in km if k not in calls]
    receipt_p = Path(out_dir) / "priority_receipt.jsonl"
    with open(receipt_p, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "driver_sha256": G["file_sha256"](Path(__file__).resolve()),
            "arms": list(COMMON_ARMS), "pass_new_calls": done,
            "checkpoint_before": before, "checkpoint_after": after,
            "common_remaining": len(remaining),
            "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}) + "\n")
    if remaining:
        print(f"[priority] INCOMPLETE: {len(remaining)} common-arm calls pending", flush=True)
        sys.exit(2)
    print(f"[priority] COMMON ARMS COMPLETE ({len(km)} calls; full judge still "
          f"requires the remaining arms)", flush=True)


def summarize(judge, out_dir):
    spec = G["JUDGES"][judge]
    items = G["load_items"]()
    G["verify_manifest"](items)
    out, ck = G["open_checkpoint"](judge, out_dir)
    km = common_keymap(judge, items)
    missing = [k for k in km if k not in ck["calls"]]
    if missing:
        sys.exit(f"{len(missing)} common-arm calls missing - not summarizable")
    from collections import Counter, defaultdict
    parse_counts, model_counts, transport = Counter(), Counter(), Counter()
    per_record = defaultdict(dict)
    for key, (item, metric, prompt) in km.items():
        call = ck["calls"][key]
        if not call.get("completion"):
            sys.exit(f"raw completion missing at {key}")
        rating, status = G["parse_rating_info"](call["completion"])
        if rating != call.get("rating") or status != call.get("parse_status"):
            sys.exit(f"reparse mismatch at {key}")
        if not 0.0 <= float(rating) <= 2.0:
            sys.exit(f"out-of-range rating at {key}")
        want = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        if call.get("request_prompt_sha256") != want:
            sys.exit(f"prompt hash mismatch at {key}")
        if call.get("returned_model") != spec["model"]:
            sys.exit(f"returned_model {call.get('returned_model')!r} != {spec['model']} at {key}")
        if not call.get("response_id"):
            sys.exit(f"response_id missing at {key}")
        if not call.get("usage"):
            sys.exit(f"usage missing/empty at {key}")
        parse_counts[status] += 1
        model_counts[call.get("returned_model")] += 1
        transport[call.get("transport", "synchronous")] += 1
        per_record[item["record_key"]][metric] = (item, float(rating))
    rows = defaultdict(list)
    for rk, metrics in per_record.items():
        if len(metrics) != 3:
            sys.exit(f"record {rk[:12]} has {len(metrics)} metrics")
        item = metrics["concept"][0]
        scores = {m: metrics[m][1] for m in ("concept", "instruction", "fluency")}
        rows[(item["model_key"], item["arm_id"])].append(
            (G["harmonic"](list(scores.values())), scores["concept"],
             scores["instruction"], scores["fluency"]))
    summary = {"schema": "panel14-judge/v2/common-arm-partial", "judge": judge,
               "judge_model": spec["model"], "judge_params": spec["params"],
               "note": "four common arms only; NOT the completed full judge",
               "checkpoint_sha256": G["file_sha256"](out),
               "parse_status_counts": dict(parse_counts),
               "returned_model_counts": dict(model_counts),
               "transport_counts": dict(transport), "per_arm": {}}
    print(f"[common-arm partial] parse: {dict(parse_counts)} | models: "
          f"{dict(model_counts)} | transport: {dict(transport)}")
    print(f"{'model':<6} {'arm':<16} {'n':>4} {'harm':>6} {'conc':>6} {'instr':>6} {'flu':>6}")
    for (mk, arm), v in sorted(rows.items()):
        n = len(v)
        rec = {"n": n,
               "harmonic_mean": round(sum(x[0] for x in v) / n, 4),
               "concept_mean": round(sum(x[1] for x in v) / n, 4),
               "instruction_mean": round(sum(x[2] for x in v) / n, 4),
               "fluency_mean": round(sum(x[3] for x in v) / n, 4)}
        summary["per_arm"][f"{mk}/{arm}"] = rec
        print(f"{mk:<6} {arm:<16} {n:>4} {rec['harmonic_mean']:>6.3f} "
              f"{rec['concept_mean']:>6.3f} {rec['instruction_mean']:>6.3f} "
              f"{rec['fluency_mean']:>6.3f}")
    sp = Path(out_dir) / f"panel14_judge_{judge}.common_arms.summary.json"
    G["atomic_json"](sp, summary)
    print(f"wrote {sp}")


ap = argparse.ArgumentParser()
ap.add_argument("--run", action="store_true")
ap.add_argument("--summarize", action="store_true")
ap.add_argument("--judge", default="gpt4omini", choices=sorted(G["JUDGES"]))
ap.add_argument("--out-dir", default=str(HERE / "judge_out"))
ap.add_argument("--workers", type=int, default=4)
a = ap.parse_args()
if a.run:
    run(a.judge, a.out_dir, a.workers)
elif a.summarize:
    summarize(a.judge, a.out_dir)
else:
    sys.exit("pass --run or --summarize")
