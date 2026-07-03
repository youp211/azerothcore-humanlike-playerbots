#!/usr/bin/env python3
"""A/B eval: run held-out eval.jsonl prompts through Ollama models, compare voice.

Usage: eval_models.py MODEL [MODEL...] [--n 12] [--show 6]
Metrics per model: avg words, median latency, % replies <= 13 words (dataset max),
% starting lowercase (2008 lazy-caps style), % containing assistant-isms.
"""
import json, random, statistics, sys, time, urllib.request

ASSISTANT_ISMS = ["as an ai", "i'm here to", "feel free", "certainly!", "great question",
                  "i cannot", "language model", "how can i assist", "happy to help"]

def generate(model, prompt):
    req = urllib.request.Request(
        "http://localhost:11434/api/generate",
        data=json.dumps({"model": model, "prompt": prompt, "stream": False,
                         "options": {"num_predict": 60}}).encode(),
        headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=180) as r:
        d = json.load(r)
    return d["response"].strip(), time.time() - t0

def main():
    argv = sys.argv[1:]
    skip = {i + 1 for i, a in enumerate(argv) if a in ("--n", "--show")}
    args = [a for i, a in enumerate(argv) if not a.startswith("--") and i not in skip]
    n = int(next((sys.argv[i+1] for i, a in enumerate(sys.argv) if a == "--n"), 12))
    show = int(next((sys.argv[i+1] for i, a in enumerate(sys.argv) if a == "--show"), 6))
    lines = open("/home/admin/git/wow/finetune/dataset/eval.jsonl").read().splitlines()
    random.seed(7)
    picks = random.sample(lines, n)
    results = {m: [] for m in args}
    for i, line in enumerate(picks):
        ex = json.loads(line)
        prompt = ex["messages"][0]["content"]
        ref = ex["messages"][-1]["content"]
        for m in args:
            try:
                resp, dt = generate(m, prompt)
            except Exception as e:
                resp, dt = f"<ERROR {e}>", -1
            results[m].append((prompt, ref, resp, dt))
    for m in args:
        rows = results[m]
        words = [len(r[2].split()) for r in rows if r[3] >= 0]
        lats = [r[3] for r in rows if r[3] >= 0]
        low = sum(1 for r in rows if r[2][:1].islower())
        isms = sum(1 for r in rows if any(s in r[2].lower() for s in ASSISTANT_ISMS))
        short = sum(1 for w in words if w <= 13)
        print(f"\n=== {m} ===")
        print(f"avg words {statistics.mean(words):.1f} | <=13w {100*short/len(words):.0f}% | "
              f"lowercase-start {100*low/len(rows):.0f}% | assistant-isms {100*isms/len(rows):.0f}% | "
              f"median latency {statistics.median(lats):.2f}s")
    print("\n--- samples ---")
    for i in range(min(show, n)):
        p = results[args[0]][i][0]
        tail = p.split("personality, WHICH IS:")[-1][:60] if "WHICH IS:" in p else p[:60]
        print(f"\n[{i}] persona: {tail.strip()}")
        print(f"    ref: {results[args[0]][i][1]}")
        for m in args:
            print(f"    {m}: {results[m][i][2][:160]}")

if __name__ == "__main__":
    main()
