# Reader training set

`cases.jsonl` is training data for fine-tuning a Kev decision model as hive-route's reader (ROUTING.md §4.8, the `systemone` harness of §4.3). It holds 3,070 labelled candidate texts: 1,659 that two blind labellers agreed on (and a third, where it labelled them, didn't contradict), which are the training data, and the rest with both labels, kept for inspection. It is not an evaluation set. The evaluation sets stay `fixtures/scorer/portable.jsonl` and `suite.jsonl`, and nothing here comes from them (see "Leakage check").

Labels follow [`../scorer/RULES.md`](../scorer/RULES.md). The scripts that built the set are in [`scripts/reader-train/`](../../scripts/reader-train/).

## Sources

| Source | Candidates | Kept | License | Attribution |
|---|---|---|---|---|
| [SWE-rebench](https://huggingface.co/datasets/nebius/SWE-rebench) (`filtered` split) | 1,000 | 300 | CC-BY-4.0 | Nebius, *SWE-rebench* (Badertdinov et al., 2025). Issue problem statements, unchanged, from permissively licensed repositories; each case records its repository's license (`repo_license`). |
| [SWE-Gym](https://huggingface.co/datasets/SWE-Gym/SWE-Gym) (`train` split) | 20 | 5 | MIT | *SWE-Gym* (Pan et al., 2024). |
| Authored | 2,050 | 1,354 | GPL-3.0 (this repository) | Generic tasks generated for this set by Claude (Opus 1,000, Sonnet 1,000) and Codex (50, before its credits ran out), each from a theme and a target mix of facts. |

Real cases carry `provenance`: `dataset`, `license`, `instance_id`, `url`, `repo`, `upstream_pr`, `patch_files` (how many files the upstream fix changed, used only to oversample multi-file issues) and, for SWE-rebench, `repo_license`. Real texts are 200 to 4,000 characters, at most 4 per repository. Authored cases carry `generator`.

Real issues are almost all `single`, mostly `goal_only` and `none`, and never `costly`. Authored cases cover the rest: ops and deploys, migrations, research and goal-only asks, paid compute, production changes, multi-component work, and reviews (`kind: review`, 174 kept). A later half of the authored batches was skewed toward `few`/`many`, `explicit` and `independent` to balance the real issues.

## Labels

Two labellers labelled every candidate blind, from `RULES.md` and each case's `id`, `kind` and `text` only, in batches of 20, and marked each case `sure` unless the rules left some fact open:

- **A:** Claude Opus 5.5 (`claude -p`).
- **B:** Claude Sonnet 5 (`claude -p`). The plan was Codex (`codex exec`) as B; the host's Codex workspace was out of credits on 2026-10-14, so B is a different Claude model, and the pair is less independent than the portable set's Claude/Codex pair.
- **Check:** Codex (gpt-6-astra, high effort, `codex exec`), when its credits briefly returned, labelled 800 of the candidates the same blind way before running out again. On the 426 of them that A and B had kept, it gave the same four facts on 416 (98%); the 10 it labelled differently were dropped (`check_disagree`). Opus and Codex agreed on all four facts on 708 of the 800 (89%).

| | Cases |
|---|---|
| Labelled | 3,070 |
| Kept (both sure, all four facts agree, not contradicted by the check) | 1,659 (54%) |
| Both labellers agree, one not sure | 877 |
| Disagree on some fact | 505 |
| Kept by A and B, contradicted by the Codex check | 10 |
| Dropped by the leakage check | 18 |
| Duplicate text | 1 |

Agreement per fact over all 3,070: specification 2,883, verification 2,867, scope 2,902, consequence 3,066. Real issues keep less (305 of 1,020, 30%) than authored tasks (1,354 of 2,050, 66%).

Kept values:

| Fact | All kept | Real | Authored |
|---|---|---|---|
| specification | explicit 621, partial 393, goal_only 645 | 3 / 62 / 240 | 618 / 331 / 405 |
| verification | independent 608, weak 550, none 501 | 1 / 50 / 254 | 607 / 500 / 247 |
| scope | single 617, few 510, many 532 | 303 / 2 / 0 | 314 / 508 / 532 |
| consequence | reversible 1,059, costly 600 | 305 / 0 | 754 / 600 |

## Line format

`id`, `source`, `provenance` (real cases), `generator` (authored), `kind`, `text`, `facts` (the agreed labels; `null` unless kept), `labels` (each labeller's `facts`, `sure` and `note`), `status` (`kept`, `unsure`, `disagree`, `check_disagree`, `leakage`, `duplicate`) and, for kept cases, `split` (`train`, `calibration`, `development`). A `leakage` case's text is omitted.

## Splits and Kev records

`scripts/reader-train/convert.py` turns the kept cases into Kev training records. Each record is exactly the reader's request (`hiveroute.scorer`): the state `Task kind: <kind>\n\nTask text:\n<text>` and the questions of `systemone_questions`, with a `label` on each. Verification is two questions as the reader asks it: `verification.checked` (`noul`) is false for `none`, and then `verification.controlled` is omitted; otherwise it is true and `verification.controlled` is `independent` or `weak`.

Split per source, 85% train and 15% held out: train 1,410, calibration 126 (the temperature fit), development 123 (in-distribution evaluation).

## Leakage check

`scripts/reader-train/leakage.py` (report: `leakage.json`):

- **Instance ids, repositories, upstream PRs.** Every repository with a case in `portable.jsonl` or `portable-disputed.jsonl` (44 repositories, 49 instance ids and PRs) was excluded whole before sampling. No candidate matches any of them.
- **Text similarity** against all 281 evaluation texts (portable, portable-disputed, suite, suite-disputed with their work orders at the pinned workspace commit, and cases.jsonl): word-trigram Jaccard and TF-IDF cosine. The 18 authored candidates with cosine ≥ 0.4 or Jaccard ≥ 0.15 were dropped; by reading, they share a motif with an evaluation case (an off-by-one in `pagination.py`, a `requests` bump, "take a look at PR … tell me what you think"). The closest kept candidate has cosine 0.38 (a different load test on shared staging).
- The generator never saw the evaluation sets; it saw `RULES.md` only.

## Integrity

Version 1 of `cases.jsonl` has SHA-256 `a6e44a76157741f255cc9f18ed345cbadb399d151bd572ecef779303ab7a5387`.

## Results

Run `reader-ft-v1` (2026-10-14, host RTX 4090 laptop GPU, 16 GB), with `scripts/reader-train/night.sh`:
- **Training:** from `jaredpalmer/kev-4b@v1.0` (LoRA r16 and the pointer head), on base `Qwen/Qwen3.5-4B-Base`, 2 epochs, lr 2e-5, batch 1, accumulation 8, `--max_state 2048`. A bf16 backbone (`--weights_dtype bf16 --row_budget 2560`), after the fp32 backbone ran out of memory. About an hour.
- **Temperature:** 2.30 by minimum NLL on `calibration` (126 records), written into the run. Kev-4B 1.0's is 0.84 on the same records.
- **Held out (`development`, 123 records):** calibrated accuracy per question.

| Question | Kev-4B 1.0 | Fine-tuned |
|---|---|---|
| specification | 61% | 97% |
| verification.checked | 91% | 99% |
| verification.controlled | 70% | 98% |
| scope | 81% | 99% |
| consequence | 88% | 99% |
| All | 79% | 98% |

- **Evaluation sets** (`hive-route scorer-eval`, argmax, scored before the temperature fit, which doesn't change an argmax):

| Set | Kev-4B 1.0 | Fine-tuned | Tier lower / same / higher (fine-tuned) |
|---|---|---|---|
| `portable.jsonl` (102) | 76% | 96.3% (real issues 94%) | 0 / 99 / 3 (1.0: 10 / 77 / 15) |
| `suite.jsonl` (59) | | 96.2% | 0 / 57 / 2 |

Kev-4B 1.0 was scored in bf16, as `kev.serve` runs it; in fp32 it doesn't fit on a 16 GB GPU. The run itself (`~/opt/kev/runs/reader-ft-v1`) and the Kev records stay out of git; `convert.py` rebuilds the records from `cases.jsonl`.
