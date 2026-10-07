# Scorer test sets

Labelled tasks for checking the scorer (ROUTING.md §4.3): for each task, the right value of the four facts it estimates (`specification`, `verification`, `scope`, `consequence`). They are for comparing scorers per fact, e.g. a local Qwen through Ollama against a classifier.

There are two sets, with the same line format:

- **`suite.jsonl`: the main set (59 cases).** Two labellers agree on every label, every case is clear under the labelling rules below, and the scorer sees each case's full text.
- **`suite-disputed.jsonl` (22 cases).** Cases the second labeller labelled differently or wasn't sure of, kept with its labels (`second_label`); not for scoring.
- **`cases.jsonl`: the first, exploratory set (77 cases).** Kept as it was; its limits are described at the end.

Each line has these fields:

- `id`, `source` (`order` or `authored`) and `kind` (`work` or `review`).
- `path` (orders) or `text` (authored cases).
- `facts`: the gold labels.
- `cos_facts`: only for orders with a `Route facts:` line, recorded for comparison and never a gold label.
- `difficulty` (`clear` or `borderline`) and `notes` (why these labels).

**Orders** are real work orders from 1-hive's workspace. Each is given by a `path` relative to the workspace root, never by its text, at workspace commit `90ca75fd99ed30475c45999bb99c9ef0e1af43e7`. The text to score is the file at that commit, minus lines matching `^Route facts: ` and `^Review tier: `. **Authored** cases are short, generic software tasks written for the set.

## `suite.jsonl`

59 cases: 7 orders and 52 authored. All are `difficulty: clear`, and both labellers agree on all four facts.

| Fact | Values |
|---|---|
| specification | explicit 19, partial 17, goal_only 23 |
| verification | independent 16, weak 16, none 27 |
| scope | single 27, few 23, many 9 |
| consequence | reversible 46, costly 13 |

- **No truncation.** Every case's text, with the two lines above stripped, is at most 12,000 characters, the scorer's input limit. This was checked by script against the workspace at the pinned commit. Only 29 of the workspace's 106 orders fit.
- **Real orders cover little of the range.** They give no `goal_only`, `none`, `many` or `costly` cases, so those values rest on authored cases.

### Two labellers

The first labeller (Claude) wrote the labels and the rules below. The second, Codex with gpt-6-astra at high effort, labelled all 81 candidate cases blind, from the rules and the text only. They agreed on 76/81 (specification), 80/81 (verification), 77/81 (scope) and 80/81 (consequence). The second labeller marked 20 cases as not settled by the rules. Every case with a disagreement or an unsure mark, 22 in all, moved to `suite-disputed.jsonl`. The second labeller named these gaps in the rules, still open:
- review rules versus rule order for `specification`;
- a named defect versus a named change;
- how to count components when both a system and its parts appear;
- which point in an order's history to label;
- a measurement versus a check;
- whether permission alone, or a temporary load, makes the work costly.

## Labelling rules

Label each fact from the case text alone. Don't use outside knowledge of the codebase. The `kind` field tells you whether the task is `work` or a `review`; the rules for reviews are marked.

The four facts and their values, from the scorer's definitions:

- `specification`: `explicit` / `partial` / `goal_only`, meaning how fully the task pins what to do.
- `verification`: `independent` / `weak` / `none`, meaning what decides whether the work succeeded.
- `scope`: `single` / `few` / `many`, meaning how many components it touches.
- `consequence`: `reversible` / `costly`.

Apply the rules for each fact in the order given. Use the first one that matches.

### specification

Terms:
- A **pass/fail check** is a stated test whose outcome is yes or no. Examples: named tests must pass, an exact expected output or value, a numeric threshold, a command or health check that must succeed, or a state that must be reached. Qualitative words ("clear", "with evidence", "readable", "spot-check") are not pass/fail.
- **Saying what to change** means naming the change itself: add, remove, rename, move or implement a named thing, apply a named migration, run named steps, or set named values. Naming where a problem lives ("the endpoint is in `search/query.py`") or what the worker may touch ("you may change the DNS records") is not saying what to change.

Rules:
1. **goal_only** if the text gives only an outcome, problem or question and doesn't say what to change. Examples: "make X faster", "fix it", "find out why", "should we…?", "improve…", "get the suite passing again". This holds even if it names the area involved or gives a pass/fail check. For a review: the text only asks for an opinion ("take a look", "tell me what you think", "anything worrying?").
2. **partial** if the main deliverable is a recommendation, verdict, comparison, design or diagnosis that the worker must reach. This applies even when steps or measurements are given.
3. **explicit** if the text says what to change or lists the steps, **and** states at least one pass/fail check. How the code achieves it may be left open. For a review: the text gives pass/fail criteria to apply and the form of the verdict (approve/request changes, a pass/fail table).
4. **partial** otherwise. This covers tasks where what to do is named but no pass/fail check is stated, or where key choices (design, cache store, which option) are left to the worker. For a review: it names the change and a focus ("comment on accessibility") but gives no pass/fail criteria.

### verification

Ask what decides whether the requested work succeeded.

1. **none** if the text states no check of any kind: no tests, no expected output, no measurement, no acceptance criterion, no reviewer.
   - "Report back", "tell us", "explain why", or choosing between options the worker offers are not checks.
   - A requirement ("use the brand colors") is not a check.
   - For a review: the text gives no criteria or focus at all.
2. **independent** if the check that decides success is outside the worker's control:
   - tests or fixtures that already exist, when passing them is what the task asks for or they are marked protected or not to be edited;
   - an existing benchmark, timing script, scorer or eval harness the worker must not change;
   - a reference that already exists: the current output, an archive, or row counts or hashes read from the system;
   - health checks or monitoring of a running system, or a public service's response (e.g. the package installs from the index);
   - a person other than the worker applying a stated pass/fail checklist.

   Upgrades, ports, renames, moves and dependency bumps add no new behavior. When the existing suite is their stated check, that suite decides: **independent**. For a review: independent if the verdict is decided by existing tests the reviewer must run.
3. **weak** otherwise: a check is stated, but it is one of these:
   - tests or probes the worker writes (even if existing suites must also stay green as a regression gate);
   - the worker's own measurement, inspection or spot-check;
   - a reviewer or group judging without a pass/fail checklist;
   - qualitative criteria.

   For a review: the text gives criteria or a focus that the reviewer applies, but nothing checks the reviewer's verdict.

### scope

Count components:
- **A component** is a source module or file, a script, a doc file, a config file, a package, a service, an app, a job, a database or table, a bucket, a server, a CI pipeline or image, or an external system or account.
- **Counted as one:** a module and its own unit tests; one run of an existing script or tool, without counting the data or models it processes.
- **Not counted:** the task's own report; code the worker only reads.
- **Only named or clearly required components count.** Changes the text makes conditional ("only if…", "if needed") don't count, and neither do unnamed incidental call sites.

What to count:
1. **Work that changes things:** the components it requires changing, creating or publishing to.
2. **Work that changes nothing** (research, evaluation, a design, a review): the systems the text names as being studied, run, or (for a review) touched by the change under review.
3. **Goal-only tasks:** the places the text says are in play or may need changes.

Then: **single** = 1. **few** = 2 to 4. **many** = 5 or more, or the text says the change spans many or all services or repositories.

### consequence

**costly** if any of these holds for the task itself or, for a review, for the change it approves:
- it spends paid or external compute: cloud GPUs, paid APIs, paid instances or clusters;
- it changes production or shared state: production databases, DNS, deploys or servers; shared staging services that other teams use; secrets and credentials; shared package registries or public indexes; sending email or notifications to users;
- it deletes data;
- it is otherwise hard to undo.

**reversible** otherwise. That covers changes in a branch or a local copy; local builds, tests, runs and training on local hardware; downloading public data; read-only access; and producing documents.

## `cases.jsonl` (first set)

77 cases: 50 orders and 27 authored, with 37 marked `borderline`. The labels follow the same intent as the rules above but were made before they were written down, so a few borderline cases may differ from what the rules give.

Its limits, which `suite.jsonl` addresses:
- Its orders have no `goal_only` or `none` cases and only two `costly` ones.
- 25 of its 50 orders are longer than 12,000 characters, so the scorer doesn't see all the text the labels came from.
- It had a single labeller.

Its label distribution:

| Fact | Values |
|---|---|
| specification | explicit 36, partial 35, goal_only 6 |
| verification | independent 19, weak 46, none 12 |
| scope | single 32, few 21, many 24 |
| consequence | reversible 64, costly 13 |

Three of its orders carry `cos_facts`: cp-1, sup-1 and col-10. On col-10 they disagree with the gold labels on specification and scope.
