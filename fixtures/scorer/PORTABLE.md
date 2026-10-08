# Portable scorer set

`portable.jsonl` is a labelled test set for the scorer that any hive can use: 102 cases, each label agreed by two independent labellers. Every case carries its task text inline, so it needs no workspace, private repository or work orders. It measures how well a scorer estimates the four facts (`specification`, `verification`, `scope`, `consequence`) from a task's text.

Labels follow [`RULES.md`](RULES.md), the canonical rules for this set.

## Run it

```
hive-route scorer-eval <table> fixtures/scorer/portable.jsonl --scorer <route>
```

The summary reports accuracy per fact, overall and per `source` (each dataset, and `authored`).

## Sources

| Source | Cases | License | Attribution |
|---|---|---|---|
| [SWE-rebench](https://huggingface.co/datasets/nebius/SWE-rebench) (`filtered` split) | 22 | CC-BY-4.0 | Nebius, *SWE-rebench* (Badertdinov et al., 2025). Problem statements of GitHub issues from permissively licensed repositories; each case records its repository's license (`repo_license`). |
| [SWE-PolyBench](https://huggingface.co/datasets/AmazonScience/SWE-PolyBench) (`test` split) | 7 | MIT | Amazon Science, *SWE-PolyBench* (Rashid et al., 2025). |
| [SWE-Gym](https://huggingface.co/datasets/SWE-Gym/SWE-Gym) (`train` split) | 3 | MIT | *SWE-Gym* (Pan et al., 2024). |
| Authored | 70 | GPL-3.0 (this repository) | Written for hive-route, partly from `suite.jsonl` and `suite-disputed.jsonl` (relabelled under `RULES.md`), partly new. |

The real cases use the dataset's `problem_statement` unchanged (an issue's title and body, as written on GitHub). Each has `provenance`: `dataset`, `license`, `instance_id`, `url` (the dataset), `repo`, `upstream_pr` (the pull request that resolved it) and, for SWE-rebench, `repo_license`.

SWE-bench and SWE-bench Verified were not used: their Hugging Face dataset cards state no license (only the SWE-bench code is MIT), so redistribution of the data is not clearly allowed.

## How cases were chosen

- **Real issues (32 kept of 49).** Read from samples of the three datasets: issues of at most a few thousand characters, filtered by kind (defect reports, feature requests, refactors, failing tests, multi-file changes). An issue was kept when its labels follow from `RULES.md` without a judgement call, and dropped when it was ambiguous (a hedged fix, an unclear expected result, an unclear component count). Real issues are mostly code changes in one repository: they give no `costly` cases and few `many` ones.
- **Authored (70 kept of 74).** Generic tasks that cover what real issues lack: ops and deploys, migrations, research and goal-only asks, paid compute, production changes, and reviews (`kind: review`, 14 cases).
- Cases the two labellers didn't agree on (21) are in `portable-disputed.jsonl`, with the second labeller's labels and notes; they are not for scoring.
- Every text is at most 12,000 characters (the longest is 2,831), so the scorer sees all of it.

## Labels

| Fact | Values | Real | Authored |
|---|---|---|---|
| specification | explicit 26, partial 35, goal_only 41 | 3 / 14 / 15 | 23 / 21 / 26 |
| verification | independent 25, weak 28, none 49 | 2 / 9 / 21 | 23 / 19 / 28 |
| scope | single 58, few 28, many 16 | 25 / 6 / 1 | 33 / 22 / 15 |
| consequence | reversible 78, costly 24 | 32 / 0 | 46 / 24 |

Every value has at least 15 cases. Real issues give no `costly` cases and almost no `many` ones, so accuracy on those values rests on authored cases. Read the summary per source as well as overall.

## How labels were made

1. **Rules first.** One labeller (Claude) wrote `RULES.md`, settling six gaps a second labeller had found in the earlier rules (`README.md`, "Two labellers").
2. **First labels.** The same labeller labelled 123 candidate cases under it, citing the rule behind each label in `notes`, and checked every case a second time.
3. **Blind second labels.** Codex (gpt-6-astra, high effort) labelled all 123 blind, from `RULES.md` and each case's `kind` and `text` only. It agreed on 118/123 (specification), 121/123 (verification), 117/123 (scope) and 123/123 (consequence). It marked 18 cases as not settled by the rules.
4. **Only agreement is kept.** A case stays only if the second labeller was sure and both agree on all four facts: 102 cases. One more was dropped for an inconsistent text (`vscode-search-gitignore`).

**Open rule gaps** the second labeller named, behind most disputed cases:
- an expected example against an existing reference result;
- whether a traceback names the component to repair;
- a named feature against a desired outcome;
- which investigation steps make a diagnosis task `partial`;
- when a test topic implies a pass/fail check.

Settle them in `RULES.md` before moving a disputed case back.

**Integrity.** Version 1 of `portable.jsonl` has SHA-256 `89bb955c87b4678c83bc935429867acaf06a2399d10c06c251db0d70fd1526c3`. A hive that copies the set should record this hash next to its results; any change to the set gets a new version and hash here.

## Baseline results

Version 1, as reference points (hive-route rev 12, 2026-10-08). Each cell is facts right out of 102; "Real" is the 32 real issues only.

| Scorer | Spec | Verif | Scope | Cons | Overall | Real | Tier lower / same / higher |
|---|---|---|---|---|---|---|---|
| Kev-4B 1.0 (`systemone`, local) | 70 | 82 | 69 | 89 | 76% | 73% | 10 / 77 / 15 |
| Qwen3 8B (`ollama`, local) | 60 | 35 | 87 | 61 | 60% | 56% | 36 / 51 / 15 |

Kev-4B's answers with p ≥ 0.7 are 97% right and cover 26% of answers. Kev-4B takes 0.2 s per case and Qwen 4.4 s, on an RTX 4090 laptop GPU. "Tier lower" counts cases where the estimates give a cheaper tier than the labels: the error that matters most for routing.

## Line format

`id`, `source` (dataset name or `authored`), `provenance` (real cases only), `kind` (`work` or `review`), `text`, `facts`, `difficulty` (`clear` or `borderline`), `notes`. `portable-disputed.jsonl` adds `second_label` (`facts`, `sure`, `note`).
