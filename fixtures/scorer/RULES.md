# Labelling rules for the portable scorer set

These are the canonical rules for labelling `portable.jsonl` and any case added to it. They are self-contained: a labeller needs only this file and a case's `kind` and `text`.

The scorer (`src/hiveroute/scorer.py`) estimates four facts about a software task from its text. These rules make its definitions precise enough that two labellers reach the same value:

| Fact | Values (cheapest first) | The scorer's definition |
|---|---|---|
| `specification` | `explicit`, `partial`, `goal_only` | `explicit` if the task pins a plan or interface and an acceptance check; `partial` if it describes what to do but leaves parts of the approach or the acceptance open; `goal_only` if it states only a goal and the plan must be discovered. |
| `verification` | `independent`, `weak`, `none` | `independent` if a check the worker can't edit decides success (protected tests, a reference result, a reviewer with a stated check); `weak` if success rests on tests the worker writes or on judgment; `none` if there is no check at all. |
| `scope` | `single`, `few`, `many` | `single` for one file or component; `few` for two to four; `many` for more, or several interacting subsystems. |
| `consequence` | `reversible`, `costly` | `costly` if the work spends external compute, changes shared or production state, or is hard to undo; otherwise `reversible`. |

## General

- **Text only.** Label from the case text alone. Don't use outside knowledge of the project, and don't open links.
- **Kind.** `kind` is `work` (do something) or `review` (judge a change someone else made). Some rules differ for reviews; they are marked.
- **First match.** Within each fact, apply the rules in the order given and use the first that matches.
- **Label the current ask.** Label the task the text asks for now. Background, history, earlier attempts, actions already done, and quoted earlier discussion are context: they add no components, checks or costs. If the text revises its ask, the latest version governs.
- **Required, not optional.** Only what the task requires counts. Suggestions offered as options ("alternatively", "maybe also", "it would be nice to also"), changes made conditional ("only if…", "if needed"), and permissions ("you may…") don't count, unless the text makes them required or the requested outcome can't be reached without them.
- **Questions.** A text that asks for an answer, a decision or an opinion is asking a question, even when it mentions a possible change ("Should we replace X with a library?", "Is this intended?", "How do I…?", "Should it be called Y?").

## Terms

**Concrete expected result.** A specific input, command or set of steps together with the specific result it should give: an exact output or value, a given error or no error, a status code, a file that should be written. Examples: "`mypy --strict .` should print `Success: no issues found`"; "posting without the header should return 400, not 500"; "`f(3)` should return 4"; a test case written out in the text with its assertion; "`gen-doc --index-name x` should write `x.html`". Not concrete expected results:
- a general statement of how the software should behave, with no specific input ("plugins should be loaded once", "the field should be optional", "support uppercase hex");
- a reproduction that shows only the wrong behaviour, without saying what should happen instead (a traceback; "it prints `*`");
- an expectation left implicit ("it crashes" does not state "it should not crash").

**Pass/fail check.** A stated test whose outcome is yes or no: named tests or a CI job that must pass, an existing script or health check that must succeed, a numeric threshold, a state that must be reached, or a concrete expected result. Qualitative words ("clear", "useful", "readable", "with evidence", "spot-check") are not pass/fail. "Add tests" without saying what they assert is not a pass/fail check.

**Saying what to change.** The text names the edit itself:
- add, remove, rename, move, replace or implement a named thing (a function, method, class, parameter, option, flag, config key, field, endpoint, file, dependency, or a named new feature such as "support `{#await promise then}`" or "add a `--file` flag");
- give the code fix (the line, expression or code to put in);
- run named steps, apply a named migration, or set named values.

A fix counts when the text states it as what to do ("change X to Y", "the fix is…", "I propose replacing…", "should be moved to…"), even if politely hedged ("probably just change it to…"). It does not count when offered only as a possibility or a diagnosis ("this may be fixable by…", "one way would be…", "I think the cause is…").

**Describing a defect or a desired behaviour is not saying what to change**, however precise it is: what goes wrong, its cause, where it lives ("the bug is in `pagination.py`"), and how the software should behave instead ("should return False", "should raise `NotAuthenticatedError` for 401", "the last page should not drop an item"). Fixing a described defect is an outcome, not a named change. Naming where the worker may work ("only the `auth` service") or what they may touch is not saying what to change either.

**Delivery steps** don't say what to change: work in a branch, open a PR, deploy or release the fix, write up the result.

## specification

### Work (`kind: work`)

1. **goal_only** if the text gives only an outcome, a problem (a defect report) or a question, and doesn't say what to change. Examples: "make X faster", "fix it", "find out why", "should we…?", "this crashes when…", "get the suite passing again". This holds even if it names the area involved, the cause, a concrete expected result, or a pass/fail check.
2. **partial** if the main deliverable is a recommendation, verdict, comparison, design or diagnosis that the worker must reach. This applies even when steps or measurements are given.
3. **explicit** if the text says what to change (or lists the steps) **and** states at least one pass/fail check. How the code achieves it may be left open.
4. **partial** otherwise: what to do is named but no pass/fail check is stated, or key choices (the design, the store, which of several options, the syntax) are left to the worker.

### Review (`kind: review`)

For a review, use these rules instead of the four above.

1. **goal_only** if the text only asks for an opinion, with no focus and no criteria ("take a look", "tell me what you think", "anything worrying?").
2. **explicit** if the text gives pass/fail criteria to apply, or names existing tests whose result decides the verdict, **and** the form of the verdict (approve or request changes, approve only if…, a pass/fail table).
3. **partial** otherwise: it names a focus ("comment on accessibility", "comment on rollback risk") but no pass/fail criteria.

## verification

Ask what decides whether the requested work succeeded.

1. **none** if the text states no check of any kind: no tests, no concrete expected result, no measurement, no acceptance criterion, no reviewer.
   - "Report back", "tell us", "explain why", "let us know", or choosing between options the worker offers are not checks.
   - A requirement or a general desired behaviour ("use the brand colors", "the field should be optional", "should be loaded once") is not a check.
   - A goal restated with an adjective ("make it fast", "a more useful message") is not a check.
   - A reproduction without a stated expected result is not a check.
   - For a review: the text gives no criteria or focus at all.
2. **independent** if the check that decides success is outside the worker's control:
   - tests, test suites or CI jobs that already exist in the project, when passing them is what the task asks for, or they are marked protected or not to be edited. This includes a task whose problem is that named existing tests, CI jobs or benchmarks fail: those are its check;
   - an existing benchmark, timing script, scorer or eval harness, run against a stated target (threshold or comparison) that the result must meet;
   - a reference read from an existing system or result: the current output, an archive, row counts or hashes read from the database;
   - health checks or monitoring of a running system, or a public service's response (e.g. the package installs from the index);
   - a person other than the worker applying a stated pass/fail checklist.

   Upgrades, ports, renames, moves and dependency bumps add no new behaviour. When the existing suite is their stated check, that suite decides: **independent**.

   For a review: independent only if the verdict is decided by existing tests the reviewer must run.
3. **weak** otherwise: a check is stated, but it is one of these:
   - a concrete expected result stated in the text (including a test case written out in the text): the worker turns it into a test and judges it;
   - tests the worker writes, even if existing suites or linters must also stay green as a regression gate;
   - a measurement the worker takes with no target, or with tools the worker writes or may change (timing it, profiling, "tell me the accuracy it prints", running a benchmark to compare options);
   - the worker's own inspection or spot-check;
   - a reviewer or group judging without a pass/fail checklist;
   - qualitative criteria (named properties the result is judged on).

   For a review: the text gives criteria or a focus that the reviewer applies, but nothing checks the reviewer's verdict.

## scope

**A component** is a source module or file, a script, a doc file, a config file, a package, a service, an app, a job, a database or table, a bucket, a server, a cluster or node pool, a network, a CI pipeline or image, or an external system or account.

Counting:
- **Count what the text names.** Only named or clearly required components count. Conditional or optional changes don't, and neither do unnamed incidental call sites or the files of an unnamed fix.
- **Wholes and parts.** When the text names a whole (a service, app, package, image) and also lists parts of it as separate things to change (its model, view, template and task; three named files), count the listed parts. When it names only the whole, count the whole once.
- **Counted as one:**
  - a module and its own unit tests;
  - a file that defines a resource and the resource it defines (a Terraform file and the DNS record or instance it manages; a Dockerfile, its build context and the image it builds; a job definition and its script). Running the tool that applies the file to the resource (`terraform apply`, applying a migration to its database) adds nothing;
  - one run of an existing script or tool, without counting the data or models it processes;
  - functions, methods or classes the text places in one module.
- **Not counted:**
  - the task's own report, and outputs of a run (a trained model, a generated report);
  - code or data the worker only reads;
  - tests, tables or dashboards that appear only as the check (count them only when the task changes them);
  - background mentioned as history.
- **Nothing named.** When a defect report or request names no file, module or service, count the distinct features, commands, classes or subsystems it says are affected. One affected feature is **single**.

What to count:
1. **Work that changes things:** the components it requires changing, creating or publishing to.
2. **Work that changes nothing** (research, evaluation, a design): the systems the text names as being studied or run.
3. **Goal-only tasks:** the places the text says are in play or may need changes.
4. **Reviews:** the components touched by the change under review.

Then: **single** = 1. **few** = 2 to 4. **many** = 5 or more, or the text says the change spans many or all services, repositories or "the codebase".

## consequence

**costly** if any of these holds for the task itself or, for a review, for the change it approves when the text says that change will be applied:
- it spends paid or external compute: cloud GPUs, paid APIs, paid instances or clusters;
- it changes production or shared state: production databases, DNS, deploys, servers or clusters; shared staging services that other teams use; secrets and credentials; shared package registries or public indexes; sending email or notifications to users;
- it puts heavy load on production or shared systems (a load test against shared staging, a bulk job on production), even if temporary, because others are affected;
- it deletes data;
- it is otherwise hard to undo.

Only what the task requires counts. A permission alone ("you may run test jobs on the paid cluster") does not make the task costly; it does when the requested outcome can only be reached through it ("bring the production bill down; you may resize production instances").

**reversible** otherwise. That covers changes to code or docs in a repository with no release, deploy or publication asked for; work in a branch or a local copy; local builds, tests, runs and training on local hardware; downloading public data; read-only access, including reading production; and producing documents.

## difficulty

Mark a case `clear` if every fact follows from one rule above without judgement calls, and `borderline` if any fact needs one. Say which fact and why in `notes`.
