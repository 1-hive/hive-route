# SPDX-License-Identifier: GPL-3.0-or-later
"""Author generic task texts for the reader's training data with an LLM (Claude Opus and Sonnet).

    python3 scripts/reader-train/gen_authored.py --rules fixtures/scorer/RULES.md \
        --out <work>/authored-candidates.jsonl --batches 44 --per 25 --jobs 4

Each batch gets a theme and, per item, an intended combination of facts, a kind and a
length, sampled to balance every fact value (scope evenly over single/few/many). The
intended facts only steer variety: the training labels come from the two blind
labellers (label.py). The generator never sees the evaluation sets.
"""

import argparse
import json
import random
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from llm import call_claude, extract_json

THEMES = [
    "deploys and releases of web services", "database schema migrations and backfills",
    "research questions and technology evaluations", "goal-only asks from managers or users",
    "machine-learning training and evaluation on cloud GPUs or paid APIs",
    "production incidents and hotfixes", "changes spanning many microservices",
    "frontend features in a React or Vue app", "command-line tools and their flags",
    "data pipelines, ETL jobs and warehouses", "security fixes, secrets and credential rotation",
    "documentation and READMEs", "CI pipelines, build images and test infrastructure",
    "infrastructure as code (Terraform, Kubernetes, Helm)", "mobile apps (iOS/Android)",
    "refactors and renames across a codebase", "performance work with benchmarks",
    "bug reports with exact expected outputs", "library API design and deprecations",
    "dependency upgrades and language version ports", "DNS, TLS, CDN and networking",
    "email or notification sending to customers", "billing and payments systems",
    "search and recommendation systems", "observability: logging, metrics, alerting",
    "monorepo tooling and package publishing to public registries",
    "embedded or firmware projects", "game development", "scientific computing code",
    "internal admin tools and scripts", "access control and permissions",
    "cost reduction of cloud bills", "data deletion, retention and GDPR requests",
    "load tests and capacity planning on shared staging", "feature flags and config",
    "code reviews of pull requests (kind review)", "compiler, parser or linter projects",
    "desktop apps and plugins", "API endpoints and HTTP status codes", "cron jobs and queues",
]

SPEC = ["explicit", "partial", "goal_only"]
SCOPE = ["single", "few", "many"]
CONS = ["reversible", "costly"]
LENGTHS = [("short (60-300 characters)", 0.4), ("medium (300-900 characters)", 0.4),
           ("long (900-2500 characters, multi-paragraph, like a real ticket)", 0.2)]


def verif_options(kind, spec):
    if kind == "review":
        return {"goal_only": ["none"], "explicit": ["independent", "weak"]}.get(
            spec, ["independent", "weak", "none"])
    return ["independent", "weak"] if spec == "explicit" else ["independent", "weak", "none"]


def sample_item(rng, theme, skew=False):
    """skew (batches from --skew_from on): fewer single/goal_only/none, which real issues
    already give in plenty."""
    kind = "review" if ("review" in theme or rng.random() < 0.12) else "work"
    spec = rng.choices(SPEC, [0.45, 0.35, 0.2])[0] if skew else rng.choice(SPEC)
    opts = verif_options(kind, spec)
    ver = rng.choices(opts, [3 if v == "independent" else 2 if v == "weak" else 1 for v in opts])[0] \
        if skew else rng.choice(opts)
    scope = rng.choices(SCOPE, [0.2, 0.4, 0.4])[0] if skew else rng.choice(SCOPE)
    length = rng.choices([x for x, _ in LENGTHS], [w for _, w in LENGTHS])[0]
    return {"kind": kind, "intended": {"specification": spec, "verification": ver,
                                       "scope": scope, "consequence": rng.choice(CONS)},
            "length": length}


PROMPT = """You are writing realistic software task texts for training a classifier that reads a task and
estimates four facts about it. The labelling rules the classifier follows are below. Your texts
will be labelled later by two independent labellers who see only the kind and the text, so each
text must make its facts clear from its own words under these rules.

=== RULES ===
{rules}
=== END RULES ===

Theme for this batch: {theme}.

Write {n} distinct task texts, one per item below. For each item, write a text of the given kind
and length whose labels under the rules should be the intended facts. Requirements:
- Realistic and varied: vary the voice (terse chat message, formal ticket, issue with repro,
  manager's ask, checklist), project names, languages, frameworks, file and service names,
  numbers. Use fictional project and company names. Do not reuse well-known benchmark tasks.
- The text is only the task as a requester would write it. Never mention the facts, the rules,
  labels, tiers or words like "scope: many".
- Make the facts follow from the rules without a judgement call. For scope, name the
  components the rules count (e.g. list five or more services for "many", two to four files
  or components for "few"). For consequence, make production/shared/paid/irreversible effects
  explicit when costly, and keep everything local, in a branch, or read-only when reversible.
- Write naturally, as real requesters do. Don't add sentences whose only purpose is to tell a
  labeller a fact ("this is the only file", "all five are in play", "nothing else is touched");
  let the facts follow from ordinary details of the request.
- A "review" item asks someone to judge a change someone else made (a PR, a diff, a plan).
- Items:
{items}

Reply with a JSON array only, one object per item in order:
[{{"kind": "work"|"review", "text": "..."}}, ...]"""


def batch_prompt(rules, theme, items):
    lines = [f"  {i + 1}. kind={it['kind']}; length={it['length']}; intended facts: "
             + ", ".join(f"{k}={v}" for k, v in it["intended"].items())
             for i, it in enumerate(items)]
    return PROMPT.format(rules=rules, theme=theme, n=len(items), items="\n".join(lines))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rules", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--batches", type=int, default=44)
    ap.add_argument("--per", type=int, default=25)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--skew_from", type=int, default=44)
    ap.add_argument("--start", type=int, default=0, help="first batch index (resume)")
    a = ap.parse_args()
    rules = Path(a.rules).read_text()
    rng = random.Random(a.seed)
    plans = []
    for b in range(a.batches):
        theme = THEMES[b % len(THEMES)]
        plans.append((b, theme, [sample_item(rng, theme, b >= a.skew_from) for _ in range(a.per)]))
    done = set()
    out = Path(a.out)
    if out.exists():
        done = {json.loads(x)["batch"] for x in out.read_text().splitlines() if x.strip()}

    def run(plan):
        b, theme, items = plan
        if b < a.start or b in done:
            return b, None
        # Codex was out of credits on 2026-10-14, so both halves are Claude (Opus, Sonnet).
        gen = "claude-opus" if b % 2 == 0 else "claude-sonnet"
        prompt = batch_prompt(rules, theme, items)
        for attempt in range(3):
            try:
                reply = call_claude(prompt, model=gen.split("-")[1])
                arr = extract_json(reply)
                if isinstance(arr, list) and len(arr) == len(items):
                    break
            except Exception as e:
                print(f"batch {b} attempt {attempt}: {e}", file=sys.stderr, flush=True)
        else:
            return b, None
        recs = []
        for i, (it, g) in enumerate(zip(items, arr, strict=False)):
            if not isinstance(g, dict) or not isinstance(g.get("text"), str) or not g["text"].strip():
                continue
            recs.append({"id": f"authored:b{b:03d}-{i:02d}", "source": "authored", "kind": it["kind"],
                         "text": g["text"].strip(), "batch": b, "generator": gen, "theme": theme,
                         "intended": it["intended"]})
        return b, recs

    with ThreadPoolExecutor(a.jobs) as ex, open(out, "a") as w:
        for fut in as_completed([ex.submit(run, p) for p in plans]):
            b, recs = fut.result()
            if recs:
                for r in recs:
                    w.write(json.dumps(r) + "\n")
                w.flush()
                print(f"batch {b}: {len(recs)}", flush=True)
            elif recs is None and b not in done and b >= a.start:
                print(f"batch {b}: failed", flush=True)


if __name__ == "__main__":
    main()
