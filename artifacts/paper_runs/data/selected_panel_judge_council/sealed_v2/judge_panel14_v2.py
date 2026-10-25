# Panel-14 LM judging, v2 - rebuilt per Codex QA entries [19] and [21] on
# the audited AXB44 machinery. Reused directly from
# lab/experiments/axbench_lexicality20/judge.py: the official last-`Rating:`
# parser with fail-to-zero parse status, the strict harmonic, atomic
# checkpoints, and (for the primary judge) the exact sealed chat adapter
# (gpt-4o-mini-2024-07-18, temperature 0). Council judges use the same
# parser, keys, retries, and checkpoint format with their own transports.
#
# Fail-closed provenance ([21] items 1-2): the manifest seals the execution
# code (this runner + the imported judge.py, templates, protocol), the lab
# root, the four vendored sources with their original git paths and blob
# IDs at commit 3924034, all 3,584 record identities, the exact expected
# call-key set per judge, and the three template hashes. Run and summarize
# refuse to proceed on any drift; a checkpoint binds to the manifest hash.
#
# 429 discipline ([21] item 4): Retry-After honored, bounded backoff, a
# consecutive-429 abort threshold so a dead quota never queues thousands of
# doomed retries; failed calls are never written and stay pending.
# `--canary` runs a small number of records end to end, verifies raw
# completion, parse status, metadata, prompt hash, and resume behavior, and
# writes a canary receipt for QA.
#
# The summarizer ([21] item 3) rejects extra as well as missing calls,
# recomputes every rating/parse status from the retained raw completion,
# recomputes every request-prompt hash, and requires response metadata,
# before computing the three-score harmonic.
#
# API keys come ONLY from OPENAI_API_KEY / ANTHROPIC_API_KEY env vars.
#
# Modes:
#   --manifest                  build/refresh panel14_manifest.json
#   --canary [N]                run+verify N records (default 1) for one judge
#   --run --judge NAME          run/resume one judge (gpt4omini|haiku|luna)
#   --summarize --judge NAME    verify + summarize one judge checkpoint
import argparse, hashlib, json, os, sys, threading, time, random
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

HERE = Path(__file__).resolve().parent
LAB = Path(r"C:\Users\JPMorgan\Desktop\Algoverse\NLA Steering\nla-steering-effects\lab")
sys.path.insert(0, str(LAB))
from experiments.axbench_lexicality20.judge import (  # noqa: E402
    parse_rating_info, harmonic, atomic_json, chat as chat_gpt4omini, JudgeError,
    MODEL as PRIMARY_MODEL)
from experiments.axbench_lexicality20.axbench_judge_templates import TEMPLATES  # noqa: E402
from experiments.axbench_lexicality20.protocol import canonical_sha256, file_sha256  # noqa: E402

SCHEMA = "panel14-judge/v2"
SOURCE_COMMIT = "392403413c8580f1ccc5cf260fc04c4dfed40b62"
GIT_SOURCES = {
    "qwen_results.json": {"repo_path": "artifacts/baseline_panel14/qwen/results.json",
                          "git_blob_sha1": "5010dab7c571ea80b72294f14682cf00a5488e78"},
    "gemma_results.json": {"repo_path": "artifacts/baseline_panel14/gemma/results.json",
                           "git_blob_sha1": "f49271427ef6275bf89af62b77559397a30802a9"},
    "qwen_nla.json": {"repo_path": "artifacts/baseline_panel14/qwen_nla/transcripts.json",
                      "git_blob_sha1": "ee5a207668254b8be005c0f0f5e773b4b887d1b3"},
    "gemma_nla.json": {"repo_path": "artifacts/baseline_panel14/gemma_nla/transcripts.json",
                       "git_blob_sha1": "60f1cd4344a6ca641104bb9a426805063b185830"},
}
CODE_FILES = {
    "judge_panel14_v2.py": Path(__file__).resolve(),
    "judge.py": LAB / "experiments" / "axbench_lexicality20" / "judge.py",
    "axbench_judge_templates.py": LAB / "experiments" / "axbench_lexicality20" / "axbench_judge_templates.py",
    "protocol.py": LAB / "experiments" / "axbench_lexicality20" / "protocol.py",
}
JUDGES = {
    "gpt4omini": {"model": PRIMARY_MODEL, "params": {"temperature": 0},
                  "env": "OPENAI_API_KEY", "expect_model_prefix": "gpt-4o-mini"},
    "haiku": {"model": "claude-haiku-4-5-20251001",
              "params": {"temperature": 0, "max_tokens": 800},
              "env": "ANTHROPIC_API_KEY", "expect_model_prefix": "claude-haiku-4-5"},
    "luna": {"model": "gpt-5.6-luna",
             "params": {"reasoning_effort": "low", "max_completion_tokens": 800},
             "env": "OPENAI_API_KEY", "expect_model_prefix": "gpt-5.6-luna"},
}
CONSEC_429_ABORT = 25
ABORT = threading.Event()
_429_LOCK = threading.Lock()
_429_STREAK = [0]


def check(cond, msg):
    if not cond:
        raise JudgeError(msg)


def git_blob_sha1(path):
    data = open(path, "rb").read()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def code_hashes():
    return {name: file_sha256(p) for name, p in CODE_FILES.items()}


def load_items():
    items = []
    for mk in ("qwen", "gemma"):
        res = json.load(open(HERE / "sources" / f"{mk}_results.json", encoding="utf-8"))
        for r in res:
            items.append({"model_key": mk, "arm_id": r["method"],
                          "concept_id": int(r["concept_id"]), "concept_label": r["concept"],
                          "instruction": r["prompt"], "sample": int(r["sample_index"]),
                          "sentence": r["response"]})
        tr = json.load(open(HERE / "sources" / f"{mk}_nla.json", encoding="utf-8"))
        for r in tr:
            for i, text in enumerate(r["generation_arms"]["av_edit"]):
                items.append({"model_key": mk, "arm_id": "nla_av_edit",
                              "concept_id": int(r["concept_id"]), "concept_label": r["concept"],
                              "instruction": r["prompt"], "sample": i, "sentence": text})
    for it in items:
        it["record_key"] = canonical_sha256({
            "model_key": it["model_key"], "arm_id": it["arm_id"],
            "concept_id": it["concept_id"], "instruction": it["instruction"],
            "sample": it["sample"], "sentence": it["sentence"]})
    check(len({it["record_key"] for it in items}) == len(items), "duplicate record keys")
    return items


def prompts_for(item):
    return {
        "concept": TEMPLATES["concept"].format(concept=item["concept_label"],
                                               sentence=item["sentence"]),
        "instruction": TEMPLATES["instruction"].format(instruction=item["instruction"],
                                                       sentence=item["sentence"]),
        "fluency": TEMPLATES["fluency"].format(sentence=item["sentence"]),
    }


def call_key(judge, item, metric, prompt):
    spec = JUDGES[judge]
    return canonical_sha256({"judge": judge, "model": spec["model"],
                             "params": spec["params"], "record_key": item["record_key"],
                             "metric": metric, "prompt": prompt})


def all_call_keys(judge, items):
    keys = {}
    for item in items:
        for metric, prompt in prompts_for(item).items():
            keys[call_key(judge, item, metric, prompt)] = (item, metric, prompt)
    return keys


def keyset_sha256(keys):
    return hashlib.sha256("\n".join(sorted(keys)).encode()).hexdigest()


def template_hashes():
    return {m: hashlib.sha256(TEMPLATES[m].encode("utf-8")).hexdigest()
            for m in ("concept", "instruction", "fluency")}


def manifest_path():
    return HERE / "panel14_manifest.json"


def build_manifest():
    items = load_items()
    sources = {}
    for f, meta in GIT_SOURCES.items():
        p = HERE / "sources" / f
        blob = git_blob_sha1(p)
        check(blob == meta["git_blob_sha1"],
              f"{f}: blob {blob} != recorded {meta['git_blob_sha1']}")
        sources[f] = {"sha256": file_sha256(p), "source_commit": SOURCE_COMMIT, **meta}
    man = {
        "schema": SCHEMA + "/manifest",
        "source_commit": SOURCE_COMMIT,
        "lab_root": str(LAB),
        "code_sha256": code_hashes(),
        "source_files": sources,
        "template_sha256": template_hashes(),
        "n_records": len(items),
        "n_calls_per_judge": len(items) * 3,
        "record_keys_sha256": hashlib.sha256(
            "\n".join(sorted(it["record_key"] for it in items)).encode()).hexdigest(),
        "call_keys_sha256": {j: keyset_sha256(all_call_keys(j, items)) for j in JUDGES},
        "judges": JUDGES,
    }
    atomic_json(manifest_path(), man)
    print(f"manifest: {len(items)} records, {len(items)*3} calls/judge, "
          f"code+sources+call-key set sealed")
    return man


def verify_manifest(items):
    check(manifest_path().exists(), "manifest missing; run --manifest first")
    man = json.loads(manifest_path().read_text(encoding="utf-8"))
    check(man.get("schema") == SCHEMA + "/manifest", "manifest schema drift")
    check(man.get("source_commit") == SOURCE_COMMIT, "source commit drift")
    check(man.get("lab_root") == str(LAB), "lab root drift")
    check(man.get("code_sha256") == code_hashes(),
          "EXECUTION CODE DRIFT: runner/judge.py/templates/protocol changed "
          "since the manifest was sealed; rebuild manifest deliberately")
    for f, meta in man["source_files"].items():
        p = HERE / "sources" / f
        check(file_sha256(p) == meta["sha256"], f"source drift: {f}")
        check(git_blob_sha1(p) == meta["git_blob_sha1"], f"blob drift: {f}")
    check(man["template_sha256"] == template_hashes(), "template drift")
    check(man["n_records"] == len(items), "record count drift")
    check(man["n_calls_per_judge"] == len(items) * 3, "call count drift")
    got = hashlib.sha256("\n".join(sorted(it["record_key"] for it in items)).encode()).hexdigest()
    check(got == man["record_keys_sha256"], "record identity drift")
    check(set(man["judges"]) == set(JUDGES) and all(
        man["judges"][j]["model"] == JUDGES[j]["model"]
        and man["judges"][j]["params"] == JUDGES[j]["params"] for j in JUDGES),
        "judge definition drift")
    for j in JUDGES:
        check(man["call_keys_sha256"][j] == keyset_sha256(all_call_keys(j, items)),
              f"call-key set drift for {j}")
    return man


def response_record(completion, payload):
    rating, parse_status = parse_rating_info(completion)
    return {"completion": completion, "rating": rating, "parse_status": parse_status,
            "returned_model": payload.get("model"), "usage": payload.get("usage", {}),
            "response_id": payload.get("id"),
            "completed_utc": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def chat_haiku(prompt, api_key, *, timeout=120):
    spec = JUDGES["haiku"]
    body = json.dumps({"model": spec["model"], **spec["params"],
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = Request("https://api.anthropic.com/v1/messages", data=body, method="POST",
                  headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                           "Content-Type": "application/json"})
    with urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return response_record("".join(b.get("text", "") for b in payload["content"]).strip(),
                           payload)


def chat_luna(prompt, api_key, *, timeout=180):
    spec = JUDGES["luna"]
    body = json.dumps({"model": spec["model"], **spec["params"],
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = Request("https://api.openai.com/v1/chat/completions", data=body, method="POST",
                  headers={"Authorization": f"Bearer {api_key}",
                           "Content-Type": "application/json"})
    with urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return response_record(str(payload["choices"][0]["message"]["content"] or "").strip(),
                           payload)


CHAT = {"gpt4omini": chat_gpt4omini, "haiku": chat_haiku, "luna": chat_luna}


def note_429(hit):
    with _429_LOCK:
        if hit:
            _429_STREAK[0] += 1
            if _429_STREAK[0] >= CONSEC_429_ABORT:
                ABORT.set()
        else:
            _429_STREAK[0] = 0


def call_with_retry(judge, prompt, api_key, key, errors):
    for attempt in range(6):
        if ABORT.is_set():
            raise JudgeError(f"aborted (consecutive-429 threshold) before {key}")
        try:
            response = CHAT[judge](prompt, api_key)
            response["retry_count"] = attempt
            note_429(False)
            return key, response
        except HTTPError as exc:
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            errors.append({"key": key, "class": "HTTPError", "code": exc.code,
                           "retry_after": retry_after, "attempt": attempt,
                           "when": datetime.now(timezone.utc).isoformat(timespec="seconds")})
            if exc.code == 429:
                # count every 429 response toward the abort threshold
                note_429(True)
            if attempt == 5:
                raise JudgeError(f"judge call failed after retries for {key}: HTTP {exc.code}") from exc
            if exc.code == 429 and retry_after:
                try:
                    time.sleep(min(120.0, float(retry_after)))
                    continue
                except ValueError:
                    pass
            time.sleep(min(30.0, (2 ** attempt) + random.Random(key + str(attempt)).random()))
        except (URLError, TimeoutError, KeyError, json.JSONDecodeError) as exc:
            errors.append({"key": key, "class": type(exc).__name__, "code": None,
                           "attempt": attempt,
                           "when": datetime.now(timezone.utc).isoformat(timespec="seconds")})
            if attempt == 5:
                raise JudgeError(f"judge call failed after retries for {key}: {exc}") from exc
            time.sleep(min(30.0, (2 ** attempt) + random.Random(key + str(attempt)).random()))
    raise AssertionError("unreachable")


def checkpoint_path(judge, out_dir):
    return Path(out_dir) / f"panel14_judge_{judge}.json"


def open_checkpoint(judge, out_dir):
    spec = JUDGES[judge]
    out = checkpoint_path(judge, out_dir)
    existing = json.loads(out.read_text(encoding="utf-8")) if out.exists() else {
        "schema": SCHEMA, "judge": judge, "judge_model": spec["model"],
        "judge_params": spec["params"], "code_sha256": code_hashes(),
        "manifest_sha256": file_sha256(manifest_path()), "calls": {}, "errors": []}
    identity = (existing.get("schema"), existing.get("judge"),
                existing.get("judge_model"), existing.get("judge_params"))
    check(identity == (SCHEMA, judge, spec["model"], spec["params"]),
          "checkpoint identity drift")
    check(existing.get("manifest_sha256") == file_sha256(manifest_path()),
          "checkpoint bound to a different manifest")
    check(existing.get("code_sha256") == code_hashes(),
          "checkpoint bound to different execution code")
    existing.setdefault("errors", [])
    return out, existing


def run(judge, out_dir, workers, limit_records=None, canary=False):
    spec = JUDGES[judge]
    api_key = os.environ.get(spec["env"], "")
    check(bool(api_key), f"{spec['env']} is not set (env vars only; no secret files)")
    items = load_items()
    verify_manifest(items)
    if limit_records:
        items = items[:limit_records]
    out, existing = open_checkpoint(judge, out_dir)
    calls, errors = existing["calls"], existing["errors"]
    keymap = all_call_keys(judge, items)
    pending = [(k, prompt) for k, (item, metric, prompt) in keymap.items() if k not in calls]
    pending_prompts = dict(pending)
    print(f"[{judge}] records={len(items)} total={len(items)*3} "
          f"cached_relevant={len(keymap)-len(pending)} pending={len(pending)}", flush=True)
    ABORT.clear()
    _429_STREAK[0] = 0
    t0, done = time.time(), 0
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = {pool.submit(call_with_retry, judge, prompt, api_key, key, errors): key
                   for key, prompt in pending}
        completed_since_save = 0
        cancelled = False
        for future in as_completed(futures):
            if ABORT.is_set() and not cancelled:
                cancelled = True
                for f in futures:
                    f.cancel()
            if future.cancelled():
                continue
            try:
                key, response = future.result()
            except JudgeError as exc:
                if not ABORT.is_set() or "aborted" not in str(exc):
                    print(f"[{judge}] PENDING-KEPT: {exc}", flush=True)
                continue
            response["request_prompt_sha256"] = hashlib.sha256(
                pending_prompts[key].encode("utf-8")).hexdigest()
            calls[key] = response
            done += 1
            completed_since_save += 1
            if completed_since_save >= 50:
                atomic_json(out, existing)
                completed_since_save = 0
                print(f"[{judge}] {done}/{len(pending)} new, {time.time()-t0:.0f}s", flush=True)
    finally:
        pool.shutdown(wait=True)
        atomic_json(out, existing)
    if ABORT.is_set():
        print(f"[{judge}] ABORTED after {CONSEC_429_ABORT} consecutive 429 responses; "
              f"{done} new calls cached; the rest stay pending", flush=True)
    print(f"[{judge}] pass done: {done} new; checkpoint {out}", flush=True)
    if canary:
        return canary_verify(judge, out_dir, items, keymap)
    remaining = [k for k in keymap if k not in calls]
    if remaining:
        print(f"[{judge}] INCOMPLETE: {len(remaining)}/{len(keymap)} calls still pending; "
              f"resume with the same command", flush=True)
        sys.exit(2)
    print(f"[{judge}] COMPLETE: all {len(keymap)} calls cached", flush=True)


def canary_verify(judge, out_dir, items, keymap):
    spec = JUDGES[judge]
    out, existing = open_checkpoint(judge, out_dir)
    calls = existing["calls"]
    receipt = {"schema": SCHEMA + "/canary", "judge": judge,
               "judge_model": spec["model"], "judge_params": spec["params"],
               "manifest_sha256": file_sha256(manifest_path()),
               "code_sha256": code_hashes(),
               "n_records": len(items), "n_calls": len(keymap), "calls": {}}
    for key, (item, metric, prompt) in keymap.items():
        call = calls.get(key)
        check(call is not None, f"canary call missing: {metric} for {item['record_key'][:12]}")
        check(bool(call.get("completion")), f"canary raw completion empty at {key}")
        rating, status = parse_rating_info(call["completion"])
        check(rating == call.get("rating") and status == call.get("parse_status"),
              f"canary reparse mismatch at {key}")
        check(str(call.get("returned_model", "")).startswith(spec["expect_model_prefix"]),
              f"canary returned_model {call.get('returned_model')!r} unexpected")
        check(bool(call.get("response_id")), f"canary response_id missing at {key}")
        check(bool(call.get("usage")), f"canary usage missing at {key}")
        want = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        check(call.get("request_prompt_sha256") == want, f"canary prompt hash mismatch at {key}")
        receipt["calls"][key] = {"metric": metric, "rating": call["rating"],
                                 "parse_status": call["parse_status"],
                                 "returned_model": call["returned_model"],
                                 "response_id": call["response_id"],
                                 "retry_count": call.get("retry_count"),
                                 "record_key": item["record_key"]}
    # resume check: nothing should be pending for these keys
    still = [k for k in keymap if k not in calls]
    check(not still, f"{len(still)} canary keys still pending after run")
    receipt["resume_check"] = "all canary keys cached; second pass would submit 0 calls"
    receipt["checkpoint_sha256"] = file_sha256(out)
    rp = Path(out_dir) / f"canary_receipt_{judge}.json"
    atomic_json(rp, receipt)
    print(f"CANARY PASSED for {judge}: {len(keymap)} calls verified -> {rp}")


def summarize(judge, out_dir):
    spec = JUDGES[judge]
    items = load_items()
    verify_manifest(items)
    out, existing = open_checkpoint(judge, out_dir)
    calls = existing["calls"]
    keymap = all_call_keys(judge, items)
    extra = [k for k in calls if k not in keymap]
    check(not extra, f"{len(extra)} extra calls not in the expected call-key set")
    missing = [k for k in keymap if k not in calls]
    check(not missing, f"{len(missing)} calls missing; judge {judge} incomplete")
    from collections import Counter, defaultdict
    parse_counts, model_counts = Counter(), Counter()
    per_record = defaultdict(dict)
    for key, (item, metric, prompt) in keymap.items():
        call = calls[key]
        check(bool(call.get("completion")), f"raw completion missing at {key}")
        rating, status = parse_rating_info(call["completion"])
        check(rating == call.get("rating"), f"stored rating != reparse at {key}")
        check(status == call.get("parse_status"), f"stored parse_status != reparse at {key}")
        check(0.0 <= float(rating) <= 2.0, f"out-of-range rating at {key}")
        want = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        check(call.get("request_prompt_sha256") == want, f"prompt hash mismatch at {key}")
        check(str(call.get("returned_model", "")).startswith(spec["expect_model_prefix"]),
              f"returned_model {call.get('returned_model')!r} unexpected at {key}")
        check(bool(call.get("response_id")), f"response_id missing at {key}")
        check(bool(call.get("usage")), f"usage missing/empty at {key}")
        parse_counts[status] += 1
        model_counts[call.get("returned_model")] += 1
        per_record[item["record_key"]][metric] = (item, float(rating))
    rows = defaultdict(list)
    for rk, metrics in per_record.items():
        check(len(metrics) == 3, f"record {rk[:12]} has {len(metrics)} metrics")
        item = metrics["concept"][0]
        scores = {m: metrics[m][1] for m in ("concept", "instruction", "fluency")}
        rows[(item["model_key"], item["arm_id"])].append(
            (harmonic(list(scores.values())), scores["concept"],
             scores["instruction"], scores["fluency"]))
    summary = {"schema": SCHEMA + "/summary", "judge": judge,
               "judge_model": spec["model"], "judge_params": spec["params"],
               "manifest_sha256": file_sha256(manifest_path()),
               "checkpoint_sha256": file_sha256(out),
               "parse_status_counts": dict(parse_counts),
               "returned_model_counts": dict(model_counts),
               "per_arm": {}}
    print(f"[{judge}] parse: {dict(parse_counts)} | returned models: {dict(model_counts)}")
    print(f"{'model':<6} {'arm':<18} {'n':>4} {'harm':>6} {'conc':>6} {'instr':>6} {'flu':>6}")
    for (mk, arm), v in sorted(rows.items()):
        n = len(v)
        rec = {"n": n,
               "harmonic_mean": round(sum(x[0] for x in v) / n, 4),
               "concept_mean": round(sum(x[1] for x in v) / n, 4),
               "instruction_mean": round(sum(x[2] for x in v) / n, 4),
               "fluency_mean": round(sum(x[3] for x in v) / n, 4)}
        summary["per_arm"][f"{mk}/{arm}"] = rec
        print(f"{mk:<6} {arm:<18} {n:>4} {rec['harmonic_mean']:>6.3f} "
              f"{rec['concept_mean']:>6.3f} {rec['instruction_mean']:>6.3f} "
              f"{rec['fluency_mean']:>6.3f}")
    sp = Path(out_dir) / f"panel14_judge_{judge}.summary.json"
    atomic_json(sp, summary)
    print(f"wrote {sp}")


ap = argparse.ArgumentParser()
ap.add_argument("--manifest", action="store_true")
ap.add_argument("--canary", nargs="?", const=1, type=int, default=None)
ap.add_argument("--run", action="store_true")
ap.add_argument("--summarize", action="store_true")
ap.add_argument("--judge", choices=sorted(JUDGES))
ap.add_argument("--out-dir", default=str(HERE / "judge_out"))
ap.add_argument("--workers", type=int, default=4)
args = ap.parse_args()
os.makedirs(args.out_dir, exist_ok=True)
if args.manifest:
    build_manifest()
elif args.canary is not None:
    check(args.judge is not None, "--canary needs --judge")
    run(args.judge, args.out_dir, workers=1, limit_records=args.canary, canary=True)
elif args.run:
    check(args.judge is not None, "--run needs --judge")
    run(args.judge, args.out_dir, args.workers)
elif args.summarize:
    check(args.judge is not None, "--summarize needs --judge")
    summarize(args.judge, args.out_dir)
else:
    sys.exit("pass --manifest, --canary, --run, or --summarize")
