# OpenAI Batch API transport for the panel-14 primary judge, at a collaborator's
# direction, per the Codex QA rulings in entries [40]/[41].
#
# Label ruling ([41]): this is an "AxBench-protocol 4o-mini re-score via
# the official OpenAI Batch API" - not described as benchmark-exact; the
# receipt discloses that 350 calls were synchronous and the remainder used
# Batch.
#
# Fail-closed rules implemented from [41]:
# - Deterministic sequential chunks of at most CHUNK pending call keys
#   (sorted key order), never concurrent: a new submission is refused
#   while any prior batch is nonterminal or completed-but-not-ingested.
# - Every exact input JSONL, raw output JSONL, and raw error JSONL is
#   saved under judge_out/batch_files/ and sha256-recorded, along with
#   this tool's own hash at submission time.
# - Ingest rejects unknown AND duplicate output custom_ids, requires the
#   exact returned model gpt-4o-mini-2024-07-18 on every line, merges
#   only status-200 lines, and flags each record "transport": "batch".
#   Failed/missing lines stay pending. Cached calls are never resubmitted.
# - Batch metadata is an append-only archive (one entry per batch id),
#   including the cancelled zero-request batch for the audit record.
#
# Request bodies byte-match the sealed synchronous adapter: {model,
# temperature, messages} only. custom_id = the canonical call key. The
# sealed runner file judge_panel14_v2.py is not modified (its code hash is
# manifest-bound); its functions are loaded by exec.
#
# Modes (all take --judge, default gpt4omini):
#   --submit   submit the next chunk (refused while one is open)
#   --poll     refresh the open batch's status
#   --ingest   download+retain raw files, verify, merge the open batch
#   --drive    loop submit -> poll -> ingest until no calls are pending
#   --tally    transport tally over the checkpoint (for the audit receipt)
import argparse, io, json, os, sys, time, uuid, hashlib
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

HERE = Path(__file__).resolve().parent
CHUNK = 4000
EXPECT_MODEL = "gpt-4o-mini-2024-07-18"
TERMINAL = ("completed", "failed", "expired", "cancelled")

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
load_items = _ns["load_items"]
verify_manifest = _ns["verify_manifest"]
all_call_keys = _ns["all_call_keys"]
open_checkpoint = _ns["open_checkpoint"]
atomic_json = _ns["atomic_json"]
file_sha256 = _ns["file_sha256"]
parse_rating_info = _ns["parse_rating_info"]
JUDGES = _ns["JUDGES"]

API = "https://api.openai.com/v1"


def die(msg):
    sys.exit(f"BATCH GATE: {msg}")


def key_env():
    k = os.environ.get("OPENAI_API_KEY", "")
    if not k:
        die("OPENAI_API_KEY is not set")
    return k


def api(path, apikey, data=None, headers=None, method=None, raw=False):
    h = {"Authorization": f"Bearer {apikey}"}
    if headers:
        h.update(headers)
    req = Request(API + path, data=data, headers=h, method=method)
    with urlopen(req, timeout=600) as r:
        b = r.read()
    return b if raw else json.loads(b.decode("utf-8"))


def upload_file(apikey, name, blob):
    boundary = uuid.uuid4().hex
    body = io.BytesIO()
    def part(s):
        body.write(s.encode("utf-8"))
    part(f"--{boundary}\r\nContent-Disposition: form-data; name=\"purpose\"\r\n\r\nbatch\r\n")
    part(f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{name}\"\r\n"
         "Content-Type: application/jsonl\r\n\r\n")
    body.write(blob)
    part(f"\r\n--{boundary}--\r\n")
    return api("/files", apikey, data=body.getvalue(),
               headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
               method="POST")


def meta_path(judge, out_dir):
    return Path(out_dir) / f"batch_{judge}.json"


def load_meta(judge, out_dir):
    p = meta_path(judge, out_dir)
    if not p.exists():
        return {"batches": []}
    m = json.loads(p.read_text(encoding="utf-8"))
    if "batches" not in m:  # migrate the single-batch v1 format
        m = {"batches": [dict(m, ingested=False)]}
    return m


def open_batch(meta):
    for b in meta["batches"]:
        if b.get("status") not in TERMINAL:
            return b
        if b.get("status") == "completed" and not b.get("ingested"):
            return b
    return None


def files_dir(out_dir):
    d = Path(out_dir) / "batch_files"
    d.mkdir(parents=True, exist_ok=True)
    return d


def submit(judge, out_dir):
    spec = JUDGES[judge]
    apikey = key_env()
    meta = load_meta(judge, out_dir)
    ob = open_batch(meta)
    if ob:
        die(f"batch {ob['batch_id']} is {ob.get('status')}/ingested={ob.get('ingested')} - "
            "poll/ingest it before submitting another")
    items = load_items()
    verify_manifest(items)
    out, ck = open_checkpoint(judge, out_dir)
    keymap = all_call_keys(judge, items)
    pending = sorted(k for k in keymap if k not in ck["calls"])
    if not pending:
        print("nothing pending; all calls cached")
        return False
    chunk = pending[:CHUNK]
    lines = []
    for k in chunk:
        item, metric, prompt = keymap[k]
        lines.append(json.dumps({
            "custom_id": k, "method": "POST", "url": "/v1/chat/completions",
            "body": {"model": spec["model"], "temperature": 0,
                     "messages": [{"role": "user", "content": prompt}]},
        }))
    blob = ("\n".join(lines) + "\n").encode("utf-8")
    n = len(meta["batches"])
    local_in = files_dir(out_dir) / f"{judge}_chunk{n:02d}_input.jsonl"
    local_in.write_bytes(blob)
    up = upload_file(apikey, local_in.name, blob)
    # [43] processed-file gate: do not create the batch until the Files API
    # reports the upload processed (prevents the validation race)
    fstat = up.get("status")
    for _ in range(24):
        if fstat == "processed":
            break
        if fstat == "error":
            die(f"uploaded file {up['id']} reached terminal error status")
        time.sleep(5)
        fstat = api(f"/files/{up['id']}", apikey).get("status")
    else:
        die(f"uploaded file {up['id']} not processed after 120s (status {fstat})")
    b = api("/batches", apikey,
            data=json.dumps({"input_file_id": up["id"],
                             "endpoint": "/v1/chat/completions",
                             "completion_window": "24h"}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
    meta["batches"].append({
        "batch_id": b["id"], "input_file_id": up["id"], "n_requests": len(chunk),
        "chunk_index": n, "local_input": str(local_in),
        "input_sha256": hashlib.sha256(blob).hexdigest(),
        "input_file_status": fstat,
        "tool_sha256": file_sha256(Path(__file__).resolve()),
        "submitted_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "status": b.get("status"), "ingested": False})
    atomic_json(meta_path(judge, out_dir), meta)
    print(f"chunk {n:02d}: batch {b['id']} submitted, {len(chunk)} requests "
          f"({len(pending)} were pending)")
    return True


def poll(judge, out_dir, quiet=False):
    apikey = key_env()
    meta = load_meta(judge, out_dir)
    ob = open_batch(meta)
    if not ob:
        if not quiet:
            print("no open batch")
        return None
    b = api(f"/batches/{ob['batch_id']}", apikey)
    ob["status"] = b["status"]
    ob["request_counts"] = b.get("request_counts", {})
    ob["output_file_id"] = b.get("output_file_id")
    ob["error_file_id"] = b.get("error_file_id")
    ob["batch_errors"] = b.get("errors")
    atomic_json(meta_path(judge, out_dir), meta)
    rc = ob["request_counts"]
    if not quiet:
        print(f"{ob['batch_id']}: {b['status']} | completed={rc.get('completed')} "
              f"failed={rc.get('failed')} total={rc.get('total')}")
    return ob


def ingest(judge, out_dir):
    spec = JUDGES[judge]
    apikey = key_env()
    meta = load_meta(judge, out_dir)
    ob = open_batch(meta)
    if not ob:
        die("no open batch to ingest")
    if ob.get("status") != "completed":
        die(f"open batch {ob['batch_id']} status is {ob.get('status')}")
    items = load_items()
    verify_manifest(items)
    out, ck = open_checkpoint(judge, out_dir)
    keymap = all_call_keys(judge, items)
    raw = api(f"/files/{ob['output_file_id']}/content", apikey, raw=True)
    local_out = files_dir(out_dir) / f"{judge}_chunk{ob['chunk_index']:02d}_output.jsonl"
    local_out.write_bytes(raw)
    ob["output_sha256"] = hashlib.sha256(raw).hexdigest()
    ob["local_output"] = str(local_out)
    if ob.get("error_file_id"):
        eraw = api(f"/files/{ob['error_file_id']}/content", apikey, raw=True)
        local_err = files_dir(out_dir) / f"{judge}_chunk{ob['chunk_index']:02d}_errors.jsonl"
        local_err.write_bytes(eraw)
        ob["error_sha256"] = hashlib.sha256(eraw).hexdigest()
        ob["local_errors"] = str(local_err)
    merged = skipped = errors = 0
    seen = set()
    for line in raw.decode("utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        k = rec.get("custom_id")
        if k not in keymap:
            die(f"unknown custom_id {k} in batch output - merge refused")
        if k in seen:
            die(f"duplicate custom_id {k} in batch output - merge refused")
        seen.add(k)
        if k in ck["calls"]:
            skipped += 1
            continue
        resp = rec.get("response") or {}
        if resp.get("status_code") != 200 or rec.get("error"):
            errors += 1
            continue  # stays pending
        body = resp["body"]
        if body.get("model") != EXPECT_MODEL:
            die(f"returned model {body.get('model')!r} != {EXPECT_MODEL} at {k} - merge refused")
        completion = str(body["choices"][0]["message"]["content"] or "").strip()
        rating, parse_status = parse_rating_info(completion)
        item, metric, prompt = keymap[k]
        ck["calls"][k] = {
            "completion": completion, "rating": rating, "parse_status": parse_status,
            "returned_model": body.get("model"), "usage": body.get("usage", {}),
            "response_id": body.get("id"),
            "completed_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "retry_count": 0, "transport": "batch",
            "request_prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        }
        merged += 1
    atomic_json(out, ck)
    ob["ingested"] = True
    ob["merged"] = merged
    ob["ingest_errors"] = errors
    atomic_json(meta_path(judge, out_dir), meta)
    remaining = [k for k in keymap if k not in ck["calls"]]
    print(f"chunk {ob['chunk_index']:02d}: merged={merged} skipped={skipped} "
          f"errors={errors} still-pending={len(remaining)}")
    return len(remaining)


def tally(judge, out_dir):
    out, ck = open_checkpoint(judge, out_dir)
    from collections import Counter
    c = Counter(v.get("transport", "synchronous") for v in ck["calls"].values())
    print(f"transport tally: {dict(c)} | total {len(ck['calls'])}")
    return dict(c)


def drive(judge, out_dir):
    for round_ in range(6):
        meta = load_meta(judge, out_dir)
        ob = open_batch(meta)
        if ob is None:
            if not submit(judge, out_dir):
                print("DRIVE COMPLETE: nothing pending")
                tally(judge, out_dir)
                return
            ob = open_batch(load_meta(judge, out_dir))
        while True:
            ob = poll(judge, out_dir, quiet=False)
            if ob is None or ob["status"] in TERMINAL:
                break
            time.sleep(120)
        if ob and ob["status"] == "completed":
            remaining = ingest(judge, out_dir)
            if remaining == 0:
                print("DRIVE COMPLETE: all calls cached")
                tally(judge, out_dir)
                return
        elif ob and ob["status"] == "cancelled" and not (ob.get("request_counts") or {}).get("completed"):
            print(f"skipping cancelled zero-yield batch {ob['batch_id']}")
            continue
        elif ob and ob["status"] == "failed" and not ob.get("retried"):
            errs = ((ob.get("batch_errors") or {}).get("data") or [])
            file_race = any(e.get("param") == "file_id" for e in errs)
            if file_race and not (ob.get("request_counts") or {}).get("completed"):
                # known validation race: file exists/processed but batch could
                # not see it yet. Recreate ONCE against the same uploaded file.
                apikey = key_env()
                meta = load_meta(judge, out_dir)
                for entry in meta["batches"]:
                    if entry["batch_id"] == ob["batch_id"]:
                        entry["retried"] = True
                time.sleep(20)
                b = api("/batches", apikey,
                        data=json.dumps({"input_file_id": ob["input_file_id"],
                                         "endpoint": "/v1/chat/completions",
                                         "completion_window": "24h"}).encode(),
                        headers={"Content-Type": "application/json"}, method="POST")
                meta["batches"].append({
                    "batch_id": b["id"], "input_file_id": ob["input_file_id"],
                    "n_requests": ob["n_requests"], "chunk_index": ob["chunk_index"],
                    "local_input": ob.get("local_input"),
                    "input_sha256": ob.get("input_sha256"),
                    "tool_sha256": file_sha256(Path(__file__).resolve()),
                    "retry_of": ob["batch_id"],
                    "submitted_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "status": b.get("status"), "ingested": False})
                atomic_json(meta_path(judge, out_dir), meta)
                print(f"chunk {ob['chunk_index']:02d}: recreated as {b['id']} "
                      f"(retry of {ob['batch_id']}, same input file)")
                continue
            die(f"batch {ob['batch_id']} ended failed - manual attention needed")
        elif ob:
            die(f"batch {ob['batch_id']} ended {ob['status']} - manual attention needed")
    die("drive round cap reached with calls still pending")


ap = argparse.ArgumentParser()
ap.add_argument("--submit", action="store_true")
ap.add_argument("--poll", action="store_true")
ap.add_argument("--ingest", action="store_true")
ap.add_argument("--drive", action="store_true")
ap.add_argument("--tally", action="store_true")
ap.add_argument("--judge", default="gpt4omini", choices=sorted(JUDGES))
ap.add_argument("--out-dir", default=str(HERE / "judge_out"))
a = ap.parse_args()
if a.submit:
    submit(a.judge, a.out_dir)
elif a.poll:
    poll(a.judge, a.out_dir)
elif a.ingest:
    ingest(a.judge, a.out_dir)
elif a.drive:
    drive(a.judge, a.out_dir)
elif a.tally:
    tally(a.judge, a.out_dir)
else:
    sys.exit("pass --submit, --poll, --ingest, --drive, or --tally")
