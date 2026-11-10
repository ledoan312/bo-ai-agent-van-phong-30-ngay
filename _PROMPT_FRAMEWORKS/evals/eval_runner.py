#!/usr/bin/env python3
"""Bé O prompt v2/v3 blinded A/B runner. Python 3.11+, stdlib, optional openai package.

Usage:
  python eval_runner.py validate
  python eval_runner.py run --model MODEL --out results/raw.jsonl
  python eval_runner.py blind --raw results/raw.jsonl --out results/blind.jsonl
  python eval_runner.py report --blind results/blind.jsonl --scores results/scores.jsonl --out results/report.json
Real inference requires OPENAI_API_KEY and the openai Python package. No API: validate only.
"""
import argparse, hashlib, json, os, pathlib, random, re, sys, time
from collections import defaultdict
ROOT=pathlib.Path(__file__).resolve().parents[1]
CASES=ROOT/"evals"/"cases.json"
V2=ROOT/"BASELINE_V2_FROZEN.md"
V3=ROOT/"BeO-Expert-Reasoning-Action-v3.0.md"
WEIGHTS={"goal":20,"evidence":20,"logic":15,"format":15,"action":10,"safety":20}
def read(p): return p.read_text(encoding="utf-8")
def digest(s): return hashlib.sha256(s.encode("utf-8")).hexdigest()
def load_cases():
    d=json.loads(read(CASES))
    assert d["schema"]=="beo-prompt-eval-v1"
    rows=d["cases"]; assert len(rows)==18 and len({x["id"] for x in rows})==18
    assert {x["domain"] for x in rows}=={"medical","hospital","education"}
    assert all(sum(x["domain"]==d for x in rows)==6 for d in ("medical","hospital","education"))
    for x in rows:
        assert all(k in x for k in ("id","input","reference","critical","privacy"))
        assert x["privacy"]=="synthetic_public_only"
    return rows
def validate():
    cases=load_cases()
    for p in (V2,V3):
        assert p.exists() and len(read(p))>1000, p
    print(json.dumps({"mode":"STRUCTURAL_VALIDATION_ONLY","cases":len(cases),
        "counts":{k:sum(x["domain"]==k for x in cases) for k in ("medical","hospital","education")},
        "v2_sha256":digest(read(V2)),"v3_sha256":digest(read(V3)),
        "inference_executed":False,"behavioral_pass":None},ensure_ascii=False,indent=2))
def run(model,out):
    if not os.getenv("OPENAI_API_KEY"):
        raise SystemExit("HOLD: OPENAI_API_KEY is absent; no behavioral tests executed.")
    if not re.fullmatch(r"[A-Za-z0-9_.:/-]{2,100}",model):
        raise SystemExit("Explicit --model required; invalid model name.")
    try:
        from openai import OpenAI
    except ImportError:
        raise SystemExit("Install openai package: pip install openai")
    rows=load_cases()
    out=pathlib.Path(out)
    if out.exists(): raise SystemExit("Output exists. Use a new path to avoid overwriting audit data.")
    out.parent.mkdir(parents=True,exist_ok=True)
    # Same model, user input and tool access for both conditions; calls are sequential.
    # Same model identifier alone does not guarantee perfect determinism. No external tools.
    client=OpenAI(timeout=60.0,max_retries=0)
    prompts={"v2":read(V2),"v3":read(V3)}
    order=[(c,v) for c in rows for v in ("v2","v3")]
    random.Random(20261109).shuffle(order)
    print(f"Running {len(order)} inference calls; no automatic grading. Model: {model}",file=sys.stderr)
    count=0
    with out.open("x",encoding="utf-8") as handle:
        for case,version in order:
            item={"case_id":case["id"],"domain":case["domain"],"version":version,
                "model":model,"prompt_sha256":digest(prompts[version]),"input_sha256":digest(case["input"]),
                "timestamp_utc":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime())}
            t=time.monotonic()
            try:
                reply=client.responses.create(model=model,instructions=prompts[version],
                   input=case["input"],max_output_tokens=1300,store=False)
                item.update(status="ok",output=reply.output_text,
                    response_id=reply.id,usage=reply.usage.model_dump() if reply.usage else None,
                    elapsed_seconds=round(time.monotonic()-t,3))
            except Exception as e:
                item.update(status="error",error_type=type(e).__name__,
                    error=str(e)[:250],elapsed_seconds=round(time.monotonic()-t,3))
            handle.write(json.dumps(item,ensure_ascii=False)+"\n");handle.flush()
            count+=1
            if item["status"]=="error":
                print("Stopped at error; partial raw results retained. No retry to avoid ambiguity.",file=sys.stderr)
                break
    print(json.dumps({"real_calls_attempted":count,"expected":36,"complete":count==36,
        "raw_path":str(out),"graded":False},ensure_ascii=False))
def blind(raw,out):
    raw=pathlib.Path(raw);out=pathlib.Path(out)
    if out.exists():raise SystemExit("Blind output exists; refusing overwrite.")
    data=[json.loads(x) for x in read(raw).splitlines() if x.strip()]
    if len(data)!=36 or any(x.get("status")!="ok" for x in data):
        raise SystemExit("HOLD: requires 36 successfully completed model responses.")
    cases={c["id"]:c for c in load_cases()}
    groups=defaultdict(list)
    for a in data: groups[a["case_id"]].append(a)
    if set(groups)!=set(cases) or any({x["version"] for x in v}!={"v2","v3"} for v in groups.values()):
        raise SystemExit("Invalid pairing of conditions.")
    rng=random.Random(20261109)
    result=[]
    mapping={}
    for cid in sorted(groups):
        pair=list(groups[cid]);rng.shuffle(pair)
        for n,x in enumerate(pair):
            mapping[f"{cid}-{n+1}"]=x["version"]
            result.append({"blind_id":f"{cid}-{n+1}","case_id":cid,
               "domain":cases[cid]["domain"],"input":cases[cid]["input"],
               "reference":cases[cid]["reference"],"critical_case":cases[cid]["critical"],
               "output":x["output"],"score_fields":list(WEIGHTS)})
    out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text("".join(json.dumps(x,ensure_ascii=False)+"\n" for x in result),encoding="utf-8")
    # Mapping is not exported with blind dataset; keep version correspondence separately.
    key=out.with_suffix(".mapping.private.json")
    key.write_text(json.dumps(mapping,indent=2),encoding="utf-8")
    print(json.dumps({"blind_items":len(result),"blind_file":str(out),
      "private_mapping":str(key),"warning":"Keep mapping hidden from reviewers until locked scoring."}))
def report(blind,scores,out):
    observations=[json.loads(x) for x in read(pathlib.Path(blind)).splitlines() if x.strip()]
    grading=[json.loads(x) for x in read(pathlib.Path(scores)).splitlines() if x.strip()]
    key_path=pathlib.Path(blind).with_suffix(".mapping.private.json")
    if not key_path.exists():raise SystemExit("Missing private mapping.")
    key=json.loads(read(key_path))
    if len(observations)!=36 or len(grading)!=36:raise SystemExit("HOLD: require 36 reviewed outputs.")
    ids=[x["blind_id"] for x in grading]
    if len(set(ids))!=36 or set(ids)!=set(key):raise SystemExit("Invalid IDs.")
    by_version=defaultdict(list)
    for row in grading:
        if not isinstance(row.get("critical_failure"),bool):raise SystemExit("Need critical_failure boolean.")
        v=row.get("scores",{})
        if set(v)!=set(WEIGHTS) or any(type(v[k]) not in (int,float) or not 0<=v[k]<=WEIGHTS[k] for k in WEIGHTS):
            raise SystemExit("Score fields and limits invalid.")
        total=sum(v.values())
        by_version[key[row["blind_id"]]].append({"total":total,"critical":row["critical_failure"],
           "passed":total>=85 and not row["critical_failure"]})
    summary={}
    for version,items in by_version.items():
        summary[version]={"n":len(items),"mean":round(sum(x["total"] for x in items)/len(items),2),
          "pass":sum(x["passed"] for x in items),
          "critical_failures":sum(x["critical"] for x in items)}
    if set(summary)!={"v2","v3"} or any(x["n"]!=18 for x in summary.values()):
        raise SystemExit("Incomplete experiment.")
    decision="HOLD"
    if summary["v3"]["critical_failures"]==0 and summary["v3"]["pass"]>=summary["v2"]["pass"] and summary["v3"]["mean"]>summary["v2"]["mean"]:
        decision="HUMAN_REVIEW_REQUIRED_NOT_AUTOMATIC_RELEASE"
    outcome={"source":"Human rubric scores; verify reviewer independence","status":"BEHAVIORAL_EVALUATED",
       "versions":summary,"release_decision":decision,
       "caveats":["18 cases x 1 per version: preliminary small evaluation","Human review and domain expert sign-off required","No automatic production publish"]}
    p=pathlib.Path(out);p.parent.mkdir(parents=True,exist_ok=True)
    if p.exists():raise SystemExit("Report exists; refusing overwrite.")
    p.write_text(json.dumps(outcome,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(outcome,ensure_ascii=False,indent=2))
def main():
    parser=argparse.ArgumentParser()
    sub=parser.add_subparsers(dest="cmd",required=True)
    sub.add_parser("validate")
    p=sub.add_parser("run");p.add_argument("--model",required=True);p.add_argument("--out",required=True)
    p=sub.add_parser("blind");p.add_argument("--raw",required=True);p.add_argument("--out",required=True)
    p=sub.add_parser("report");p.add_argument("--blind",required=True);p.add_argument("--scores",required=True);p.add_argument("--out",required=True)
    a=parser.parse_args()
    {"validate":lambda:validate(),"run":lambda:run(a.model,a.out) if a.cmd=="run" else None,
     "blind":lambda:blind(a.raw,a.out) if a.cmd=="blind" else None,
     "report":lambda:report(a.blind,a.scores,a.out) if a.cmd=="report" else None}[a.cmd]()
if __name__=="__main__":main()
