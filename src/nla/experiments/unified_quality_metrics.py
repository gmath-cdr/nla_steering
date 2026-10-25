"""Recompute one frozen quality-pass metric across Gemma-10 final-test methods."""
from __future__ import annotations
import argparse, json
from pathlib import Path
from experiments.rescore_prompting_quality import jaccard, surface_quality, tokens
def score(response,references):
 s=surface_quality(response); sim=max((jaccard(tokens(response),tokens(r)) for r in references),default=0.0)
 return {**s,"reference_similarity":sim,"quality_pass":8<=s["word_count"]<=350 and s["repeated_trigram_rate"]<=.10 and sim>=.12}
def nested(path,arm):
 out=[]
 for row in json.loads(Path(path).read_text()):
  refs=row["generation_arms"]["unsteered"]; out += [score(r,refs) for r in row["generation_arms"][arm]]
 return out
def flat(path,method):
 rows=json.loads(Path(path).read_text()); refs={}
 for r in rows:
  if r["method"]=="unsteered": refs.setdefault((r["concept_id"],r["prompt_index"]),[]).append(r["response"])
 return [score(r["response"],refs[(r["concept_id"],r["prompt_index"])]) for r in rows if r["method"]==method]
def aggregate(samples):
 n=len(samples)
 return {"n":n,"quality_pass_rate":sum(x["quality_pass"] for x in samples)/n,"mean_reference_similarity":sum(x["reference_similarity"] for x in samples)/n,"mean_repeated_trigram_rate":sum(x["repeated_trigram_rate"] for x in samples)/n,"mean_word_count":sum(x["word_count"] for x in samples)/n}
def main():
 p=argparse.ArgumentParser(); p.add_argument("--root",default="artifacts/gemma3-12b/baseline_gemma10_2026"); p.add_argument("--out",required=True); p.add_argument("--prompting-results"); a=p.parse_args(); root=Path(a.root)
 methods={m:flat(root/"matched_panel/results.json",m) for m in ("unsteered","diffmean","caa_fixed","caa_calibrated")}
 methods["nla_av_edit"]=nested(root/"nla/transcripts.json","av_edit"); methods["sae"]=nested(root/"sae/transcripts.json","sae"); methods["loreft"]=nested(root/"reft/transcripts.json","reft")
 prompting_path=Path(a.prompting_results) if a.prompting_results else root/"prompting/results_matched_unsteered.json"
 prompting=json.loads(prompting_path.read_text()); methods["prompting"]=[{k:r[k] for k in ("word_count","repeated_trigram_rate","reference_similarity","quality_pass")} for r in prompting]
 payload={"protocol":"frozen-final-test-quality-v1","definition":"8-350 words; repeated-trigram rate <=0.10; content-word Jaccard >=0.12 to closest prompt-matched unsteered answer from the same method run","methods":{m:aggregate(v) for m,v in methods.items()}}
 out=Path(a.out); out.parent.mkdir(parents=True,exist_ok=True); out.write_text(json.dumps(payload,indent=2)+"\n")
 out.with_suffix(".md").write_text("# Unified final-test quality\n\n| Method | n | Pass | Similarity | Repeated trigram |\n|---|---:|---:|---:|---:|\n"+"".join(f"| {m} | {v['n']} | {v['quality_pass_rate']:.1%} | {v['mean_reference_similarity']:.3f} | {v['mean_repeated_trigram_rate']:.3f} |\n" for m,v in payload["methods"].items()))
 print(out)
if __name__=="__main__": main()
