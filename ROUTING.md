# hive-route — a slim routing service for One Hive (R8)

**Status:** proposal · 2026-09-25 · rev 2 (attempt ids, indeterminate outcomes, usage basis, placement)
**Release:** R8 in the incremental plan: *named routes, one canary per route with a recorded lesson, detectable model switches.* It works on its own, logs to the record (R1) once one exists, and plugs into the worker runtime (R6). Any hive can adopt it, whether its models are paid per call through API keys, covered by subscriptions, run locally, or a mix.

---

## 1. What it does

For each unit of work, it picks which model runs it. The goals:

1. **Get more accepted work from limited capacity,** whether that capacity is money, a subscription's usage limits or local hardware. Cheap models do bounded work; strong models are kept for the decisions that need them.
2. **Make model choice visible.** Every choice, and every change to a model underneath a route, is recorded and explainable.
3. **Never trade quality for capacity silently.** When no suitable route has capacity, the work waits or is escalated. It is never quietly run on a weaker model.
4. **Keep deterministic work out of models.** Polling, parsing, hashing, running tests and comparing metrics are scripts. They are never routed, and they never count as "cheap model work".

**It does not do these; other releases or components own them:**

| Concern | Owner |
|---|---|
| Task state, authority, audit log | R1/R2, the record |
| Independent review | R5, and the record's review-gated close |
| Launching workers, restart context | R6, the worker runtime (or a hive's own launcher) |
| Replay, shadow trials, evaluation | R9, the instrument layer |
| Enforcing hard spending caps | The gateway (§8) or the provider. The router *steers by* usage; it is not an accounting authority. |

---

## 2. Concepts

- **Route.** A named, pinned model configuration: `route_id`, `tier`, `pool`, harness or endpoint, exact model id, model `family` (used by RT4), settings (reasoning effort, output limit, tool mode), capabilities (context limit, tool use), and for metered pools its price. A route is content-addressed: changing any field makes a new route version, so a model switch is always a visible change.
- **Effort is a lever too.** The same model at lower reasoning effort can be a separate route in a lower tier. A cheaper setting is often a better saving than a different model.
- **Tier.** One of `local`, `light`, `standard`, `strong`. Tiers are policy labels, not a capability ranking of every model. The router computes each attempt's tier from facts about the task (§4).
- **Pool.** A source of capacity, with limits over windows. Three kinds:

  | Kind | Example | Limits | How usage is known |
  |---|---|---|---|
  | `metered` | an API key | money (or tokens) per period, per task, per attempt | exactly, from gateway logs and prices |
  | `subscription` | a Claude, ChatGPT or GLM plan | the provider's usage windows, e.g. 5-hour and weekly | estimated; the provider's limit responses are the ground truth |
  | `local` | a GPU running Ollama or vLLM | concurrent requests | queue length |

- **Route table.** A pinned data file: pools, routes, the ordered routes for each tier, and the rule parameters (§4).
- **Unit of routing: one attempt.** A route is chosen when an attempt starts (a new task, a restart or a reassignment) and held until the attempt ends. There are no switches mid-attempt. A switch happens at a restart, whose context is rebuilt from the record.

---

## 3. Qualification: one canary per route

A route may serve a tier only after it passes a **canary**: a small fixed set of the hive's own past tasks with known-good outcomes, re-run on that route. A new hive without history starts from a small shared starter set.

- **Statuses:** `candidate` → `qualified` → `retired`.
- **Every canary records its result and a one-paragraph lesson**, e.g. "good at bounded edits, loses track of multi-file refactors". The lesson feeds back into tier assignments.
- **Qualification expires** when the route's model changes. The router detects this by comparing the model id each response reports with the pinned id; a mismatch triggers `route.drift_detected` and moves the route back to `candidate`.
- **Canaries are cheap by design:** a handful of tasks per route, rerun only on a change.

---

## 4. The decision

The router makes two decisions for each attempt, both deterministic:

1. **Which tier the attempt needs**, computed from facts about the task (§4.1–4.3).
2. **Which route serves that tier**, given capacity and fit (§4.4).

`decide(request, table, pools, history) → {tier, route_id, reasons[]}` is a pure function. The same inputs always give the same answer, so every decision can be replayed. The tier is **recomputed at every attempt**: it drops when a plan or an independent check is added, and rises when attempts fail.

### 4.1 Task facts

The router decides from facts that can be checked, not from a verdict about difficulty. Most facts come from the task itself or from the record.

| Fact | Values | Source |
|---|---|---|
| `kind` | `work`, `review`, `plan`, `consult`, `triage`, `digest` | the task |
| `specification` | `explicit`, `partial`, `goal_only` | `explicit` means the task pins a plan or interface and an acceptance check; the router checks that the pins exist |
| `verification` | `independent`, `weak`, `none` | `independent` means a check the worker can't edit: protected tests, a reference result, or a reviewer with a stated check. Self-written tests are `weak` |
| `scope` | `single`, `few`, `many` | components the task touches: declared on the task, or estimated by the scorer (§4.3); corrected from the previous attempt's changes |
| `consequence` | `reversible`, `costly` | `costly` means external compute, shared or production state, or hard to undo; flagged on the task or inherited from its parent |
| `leverage` | a count | tasks blocked on this one, where the record tracks dependencies |
| `history` | earlier attempts: route, failure class | the record |
| context size, tools needed | estimate, yes/no | the runtime |

A fact that isn't supplied is `unknown`. Unknown facts take the table's conservative default (the costlier value) unless the scorer fills them.

### 4.2 From facts to tier

The tier is the **highest** tier any applicable rule requires.

| # | When | Minimum tier |
|---|---|---|
| F1 | kind `digest` / `work`, `review` / `plan`, `triage` / `consult` | `local` / `light` / `standard` / `strong` |
| F2 | `specification: goal_only` for `work` | `standard`, with the suggestion to split it into a `plan` task and `work` tasks |
| F3 | `scope: many` | `standard` |
| F4 | `verification: none` | `standard` |
| F5 | `consequence: costly` and `verification` not `independent` | `strong` |
| F6 | `leverage` at or above the table's threshold (default 3) and `verification` not `independent` | `strong` |
| F7 | kind `review` | the author's tier; at least `standard` if `consequence: costly` |
| F8 | failure history | as §5: e.g. a second `failed_check` moves one tier up |

Rule parameters and the kind defaults live in the route table, so a hive can tune them. Each decision records which rules applied.

**The creator's hint can only raise the tier.** A task's creator may ask for a higher tier, with a one-line reason. The way to get a *cheaper* tier is to change the facts: add an independent check, write the plan first, or split the task. That puts the effort where it pays: work that is well specified and independently checked is what cheap models do well. Only an operator override can go below the computed tier.

### 4.3 The scorer: filling unknown facts

Facts are often missing, especially `scope` and `specification` for tasks written in prose. The **scorer** is an optional plug-in that estimates them.

- **Input:** the task text, its pinned references and its history. **Output:** estimates for the unknown facts, each with a short reason, plus a suggested tier for comparison. Fixed schema; invalid output is discarded and the facts stay unknown.
- **It runs on one fixed route** (a local or light model), with capped output and a timeout. It is never routed itself, and its usage is counted like any other attempt.
- **It fills unknown facts only.** It never overrides a fact supplied by the task or the record, and it never sets the tier directly: its estimates go through the same rules as every other fact.
- **Shadow first.** It starts in shadow mode: its estimates are logged (`route.scored`) but not used. It goes live when replay (R9) shows that its estimates predict outcomes better than the conservative defaults.
- **It is called only when needed:** when a fact that could change the tier is unknown. Tasks with complete facts never call it.

### 4.4 Choosing the route

**Rules, in order.** Each decision records the ids of the rules that decided it.

| # | Rule |
|---|---|
| RT1 | **Override first.** An operator override, recorded as an event, wins. |
| RT2 | **Qualified and fitting only.** Eligible routes are `qualified`; their pool is not at a limit and is allowed for the project; for metered pools, the attempt's maximum cost fits the remaining budget; their context limit fits the request with margin; and they support tools if tools are needed. An unknown capability or an unknown price counts as not fitting. |
| RT3 | **The computed tier is a floor.** Never go below it to save capacity. If no eligible route in the tier (or above, where the table allows it) has capacity, return `WAIT` with the earliest time a pool frees up; the caller asks again then. The hive's supervisor or operator decides whether to escalate. |
| RT4 | **Reviews use a different model family** from the author's attempt. |
| RT5 | **Failures by class.** After a failed attempt the router acts on its failure class (§5), not on the fact that it failed. |
| RT6 | **Choose within the tier by the table's preference.** Skip pools above the soft threshold (default 80% of any limit) while others are available. Then order by the table's `prefer` setting: `order` (listed order; the default), `cost` (lowest expected cost; subscription and local routes count as free while their pool is below the soft threshold), or `headroom` (most remaining capacity; spreads load across pools). |
| RT7 | **Stay put when it works.** A restart after a crash or a nudge keeps the previous route unless the recomputed tier, RT2 or RT5 says otherwise. |

### 4.5 Patterns the rules produce

- **Plan strong, execute light.** A `plan` task produces an explicit plan and acceptance checks. The `work` tasks that implement it are then `explicit` and `independent`, so unless their scope is large they run `light`. A `light` worker that finds the plan wrong reports that through the record (blocked, needs a decision) rather than improvising on a stronger model.
- **Consultations.** A `consult` task asks a strong model one question: the decision, the alternatives, the evidence, and what a useful answer looks like. The answer is a decision or a plan with the conditions under which it becomes invalid. It needs no tools and does none of the work. Equivalent questions from different tasks are merged into one consultation. This is the cheapest way to buy strong judgment.

### 4.6 Learning from outcomes

The record holds every attempt's facts, tier, route and outcome. Replay (R9) computes success rates and usage per kind, fact profile and tier, and proposes changes to the table's rules and defaults, e.g. "`work` with `scope: few` and `verification: weak` succeeds on `light` 90% of the time; drop F4 for it". A person approves each change as a new pinned table version, so routing never drifts on its own.

---

## 5. Failure classes

Whoever handles the failed attempt (the runtime or the hive's supervisor) classifies it before asking for a new route. Each class has one remedy.

| Class | Detected by | Remedy |
|---|---|---|
| `outage` | provider error, harness crash | Same tier, another route; or wait. **Never move up a tier for an outage.** |
| `capacity` | rate limit, usage-window limit, or budget exhausted | Mark the pool at its limit until it frees up; same tier, another pool; or wait. |
| `truncated` | output-limit or context-overflow stop | Same route with larger limits if the route allows it; otherwise ask the coordinator to split the task. Not a reason to move up a tier. |
| `missing_info` | the worker blocks on a question or missing input | Get the information (from the coordinator, or a script fetches it), then retry on the same route. Not a reason to move up a tier. |
| `failed_check` | failed review or failed test | First time: same tier, with the feedback. Second time: one tier up, or a `consult` on the approach. |
| `stalled` | no new evidence across two progress checks | The hive's supervisor or operator decides; "move up a tier" is one option. Ruling out a hypothesis counts as progress. |
| `indeterminate` | timeout or crash after the call was sent, so it's unknown whether the provider ran it | Reconcile first, using the attempt id as the provider-side idempotency key where the provider supports one. Retry with a new attempt id only if the table allows the risk of paying twice. Never record it as `failed`. |

Every attempt has a stable **attempt id**. It is passed downstream as the idempotency key wherever the gateway or provider supports one, so a retried request doesn't become a second paid call.

The restart context passed to the next attempt includes each earlier attempt's route and failure class, so the next model doesn't repeat the same failure.

---

## 6. Modes

| Mode | Behavior |
|---|---|
| `fixed` | Every kind maps to one route. This is the baseline to measure against before routing is switched on. |
| `live` | `decide()` picks routes. |
| `shadow` | A second table decides alongside the live one; its choices are logged, not used. |
| `rollback` | Re-pin a previous table version. Every table change is a pinned version, so rolling back is one recorded step. |

Operator overrides (RT1) work in every mode.

---

## 7. Usage tracking

The router needs each pool's usage against its limits. By pool kind:

- **Metered:** gateway logs give tokens and cost per request, tagged with actor, task and route. RT2 checks an attempt's maximum cost against the remaining budget; the gateway's budgets are the hard stop. Gateway figures are reconciled against provider invoices, and an estimate is never recorded as a settled charge.
- **Subscription:** estimated from gateway logs or harness session files (e.g. via AgentsView). The provider's limit responses are the ground truth and override the estimate.
- **Local:** queue length and concurrent requests.

Every usage figure carries `usage_basis`: `measured` (from gateway logs), `estimated` (e.g. subscription usage from session files), or `unknown`. An `unknown` figure is never replaced with zero. For every kind, usage covers all attempts, including failed, indeterminate ones, reviews and consultations.

**Placement is a deployment choice.** The router can run privately next to one agent (the Omega architecture's default) or be shared by a hive. Sharing is what lets a hive spread several agents across one subscription's limits. Either way, agents never hold provider keys, and usage is counted where the agent can't change it.

---

## 8. Gateway: LiteLLM by default

LiteLLM is the default path. The router still works without it (**direct mode**: the launcher passes the route's model flag, e.g. `claude --model …`, `codex -m …`, or `LLM_MODEL=…` for agents configured by environment).

**What it gives any hive:**
- **Routes become aliases.** Each harness points at one endpoint and asks for its `route_id`. Changing the model behind a route is a gateway config change, not a launcher change.
- **Agents never hold provider credentials.** Each actor gets a gateway key, scoped to the aliases it may use and revocable per agent. This matches the record's per-actor credentials; real API keys and subscription logins stay with the gateway.
- **Usage comes from the gateway, not the agent.** Requests are tagged with actor, task and route, so cost and usage per task are measured instead of self-reported.
- **Hard caps for metered pools.** Gateway budgets per key and per alias stop spending before a limit is crossed, and fail closed when the budget can't be checked. This must be tested on the pinned gateway version before it is relied on.
- **Soft caps for subscriptions.** Rate limits per alias keep a pool below the provider's limits.
- **One endpoint for many models.** API models, local models and subscription models sit behind the same endpoint, and a harness can drive non-native models through it. That makes more routes possible.
- **Drift detection in one place.** The gateway sees the model id in every response.

**Rules for using it:**
- **The route table is the single source of truth.** The gateway config is generated from it; config held in the gateway's database is not used.
- **One owner for retries and fallbacks.** Automatic gateway fallbacks and hidden retries are off. The router decides model changes, at attempt boundaries. At most one transport retry happens; it is logged and counted.
- **Canaries run through the gateway,** since translation between APIs (tool calls, reasoning settings) can change how a model behaves.
- **Pin the gateway version,** and re-run the canaries when upgrading.
- **Subscription passthrough** is used only where the provider's terms allow it. Other subscription pools use direct mode.

**Cost of using it:** one more service to run, with its own database for keys and spend logs, kept separate from the record's.

---

## 9. How it fits the hive

| Release | Integration |
|---|---|
| **R1 record** | The router is an actor of class `instrument` with its own credential. It writes events in the reserved `route.` family (below). Before R1 exists, it writes the same events to a local JSONL log that can be imported later. |
| **R6 runtime** (or a hive's own launcher) | Calls `POST /route` at each attempt start and launches the harness with the answer. Records the route on the attempt. |
| **Supervision** (if the hive has a supervisor) | "Reassign to a different model" becomes "ask the router with this failure class". |
| **R5 review** | Rules F7 and RT4. |
| **R9 instruments** | Every decision is logged with its inputs, so R9 can replay history under a different route table. A **shadow table** runs alongside the live one and logs `route.shadow_decided` without acting. Evaluation belongs to R9. |
| **Agents with their own loop** (e.g. Iter) | The agent's episode start calls the router and sets its model variable; the agent loop is unchanged. |

**Events** (the `route.` family, added to the catalog by R8):

| Event | Data |
|---|---|
| `route.table_pinned` | pin(route table); the table version now in force |
| `route.mode_set` | mode; pin(table) for `fixed`, `shadow` or `rollback` |
| `route.decided` | task, attempt, facts used (and which were estimated), tier, `route_id`, route pin, `reasons[]`, basis (the record position the decision was made from) |
| `route.scored` | task, attempt, the scorer's route, fact estimates with reasons, suggested tier, shadow or live |
| `route.waiting` | task, attempt, tier, pools at a limit, earliest time one frees up |
| `route.canary_recorded` | `route_id`, status, pin(canary result), lesson |
| `route.drift_detected` | `route_id`, pinned model id, observed model id |
| `route.override` | task, `route_id`, reason (operator only) |
| `route.shadow_decided` | as `route.decided`, for the shadow table |

Result and review events carry the attempt's usage with `route_id` and `pool` (an extension field on the record's existing events), so usage can be attributed per route. With the gateway, a usage instrument fills usage from gateway logs instead of workers reporting it.

---

## 10. Route table example

A hive mixing API keys, a subscription and a local GPU:

```yaml
version: 1
soft_threshold: 0.8
prefer: cost
kind_floor: {digest: local, work: light, review: light, plan: standard, triage: standard, consult: strong}
leverage_threshold: 3
unknown_facts: conservative      # the scorer fills them once it is live
scorer: {route: qwen-local, mode: shadow, max_output_tokens: 800, timeout_s: 20}
pools:
  anthropic-api: {kind: metered, limits: [{usd: 500, per: month}, {usd: 20, per: attempt}]}
  openai-api:    {kind: metered, limits: [{usd: 300, per: month}, {usd: 20, per: attempt}]}
  claude-plan:   {kind: subscription, limits: [{window: 5h}, {window: 7d}]}   # amounts unknown; the provider signals them
  local-gpu:     {kind: local, limits: [{concurrent: 2}]}
routes:
  opus-api:      {tier: strong,   pool: anthropic-api, family: claude, model: <exact id>, effort: high, price: {in: <usd/Mtok>, out: <usd/Mtok>}}
  opus-plan:     {tier: strong,   pool: claude-plan,   family: claude, model: <exact id>, effort: high}
  gpt-high-api:  {tier: strong,   pool: openai-api,    family: gpt, model: <exact id>, effort: high, price: {in: <usd/Mtok>, out: <usd/Mtok>}}
  gpt-api:       {tier: standard, pool: openai-api,    family: gpt, model: <exact id>, effort: medium, price: {in: <usd/Mtok>, out: <usd/Mtok>}}
  sonnet-plan:   {tier: standard, pool: claude-plan,   family: claude, model: <exact id>}
  mini-api:      {tier: light,    pool: openai-api,    family: gpt, model: <exact id>, price: {in: <usd/Mtok>, out: <usd/Mtok>}}
  qwen-local:    {tier: local,    pool: local-gpu,     family: qwen, model: <exact id>}
tiers:
  strong:   [opus-plan, opus-api, gpt-high-api]   # subscription first; API when it hits its window
  standard: [sonnet-plan, gpt-api]
  light:    [mini-api]
  local:    [qwen-local]
allow_upgrade_on_wait: [local, light]  # may run one tier up rather than wait
```

A hive on API keys only lists metered pools and sets `prefer: cost`. A hive on subscriptions only lists subscription pools and may set `prefer: headroom`.

---

## 11. Build sequence

1. **Core library and CLI:** route table schema, pools, `decide()`, rule fixtures, JSONL log.
2. **Gateway:** config generated from the route table; per-actor keys; request tagging; retries and fallbacks off; budget enforcement tested.
3. **Usage tracking** for all three pool kinds.
4. **Launcher integration,** starting in `fixed` mode to capture a baseline.
5. **Canaries and drift detection;** then switch to `live`.
6. **Scorer** in shadow mode; live once replay supports it.
7. **Record events,** once R1 is running.
8. **Shadow tables** and outcome-based tuning (§4.6), with evaluation through R9.

**Deferred, each with its trigger:**

| Deferred | Trigger |
|---|---|
| Letting the scorer set tiers directly, not just fill facts | Replay shows its suggested tiers beat the fact rules |
| Automatic table changes without human approval | Many approved tuning changes in a row with no regressions |
| The router's own spend ledger | Gateway budget enforcement proves inadequate in testing or use |
| Budgets across several hives or gateways | Several hives share one pool |
| Switching models within an attempt | Measured evidence that restart handoffs are too costly |
| Multi-operator route attestation | Several operators share routes (One Hive M2) |

---

## 12. Done means

- A hive runs it for real work and files an adoption note.
- Every attempt's model can be read from the log, and every model change underneath a route is detected.
- Each route in use has a canary with a recorded lesson.
- Measured against the `fixed` baseline: more accepted tasks per unit of capacity (money for metered pools, usage share for subscriptions), fewer limit hits, and no drop in review pass rate.
