# hive-route — a slim routing service for One Hive (R8)

**Status:** built · 2026-09-30 · rev 4 (all build steps built; 1-hive runs `live`, with SingularityCompute first through the gateway)
**Release:** R8 in the incremental plan: *named routes, one canary per route with a recorded lesson, detectable model switches.* It works on its own, logs to its own JSONL log until the record admits `route.` events (§9), and plugs into the worker runtime (R6) or a hive's own launcher. Any hive can adopt it, whether its models are paid per call through API keys, covered by subscriptions, run locally, or a mix.

**Changes in rev 4:**
- Every build step now has an implementation (§11): usage tracking from providers' own reports (§7), drift checks through attempt manifests and canaries with a starter suite (§3), the scorer on a local model (§4.3), shadow tables, what-if replay and an outcomes report (§4.6, §6), a generated gateway config (§8), and recording on the hive record through its amendment A1 (§9.1).
- What remains is operational, in 1-hive (§11.1): run the canaries, apply A1 and register the router, then switch to `live`.

**Changes in rev 3:**
- R1 now exists: the record (hive-record SPEC v1.0) runs 1-hive in authoritative mode. It reserves the `route.` prefix for R8 and forbids extensions to use it, so recording routing in the record needs a spec amendment (§9.1). Until then the JSONL log is the router's log.
- `decide()` is specified exactly: its inputs, its four outcomes and the time it reads (§4, §4.7). Step 1 of the build is implemented to this contract (§11).
- Qualification is bound to the route's pin, so any change to a route voids it without a separate check (§3).
- New failure class `interrupted` (§5), which is what RT7 keeps a route for; `outage` stays for provider and harness failures.
- Usage state has a fixed shape (§7); the attempt's maximum cost comes from a per-attempt limit or the request (§4.4).
- Current deployment: what 1-hive can use today, and which build steps wait for what (§11.1).

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

- **Route.** A named, pinned model configuration: `route_id`, `tier`, `pool`, harness or endpoint, exact model id, model `family` (used by RT4), settings (reasoning effort, output limit, tool mode), capabilities (`context_limit`, `tools`), and for metered pools its price (USD per million tokens, `in` and `out`). A route is content-addressed: its **route pin** is the SHA-256 of its canonical JSON, including its id. Changing any field makes a new route version, so a model switch is always a visible change.
- **Effort is a lever too.** The same model at lower reasoning effort can be a separate route in a lower tier. A cheaper setting is often a better saving than a different model.
- **Tier.** One of `local`, `light`, `standard`, `strong`. Tiers are policy labels, not a capability ranking of every model. The router computes each attempt's tier from facts about the task (§4).
- **Pool.** A source of capacity, with limits over windows. Three kinds:

  | Kind | Example | Limits | How usage is known |
  |---|---|---|---|
  | `metered` | an API key | money (or tokens) per period, per task, per attempt | exactly, from gateway logs and prices |
  | `subscription` | a Claude, ChatGPT or GLM plan | the provider's usage windows, e.g. 5-hour and weekly | estimated; the provider's limit responses are the ground truth |
  | `local` | a GPU running Ollama or vLLM | concurrent requests | queue length |

- **Route table.** A pinned data file (`format: hive-route.table/1`, schema in `schemas/`): pools, routes, the ordered routes for each tier, the `fixed` mapping (§6) and the rule parameters (§4). Its pin is the SHA-256 of its canonical JSON with defaults filled in, so a default changing in a new router version also changes the pin.
- **Unit of routing: one attempt.** A route is chosen when an attempt starts (a new task, a restart or a reassignment) and held until the attempt ends. There are no switches mid-attempt. A switch happens at a restart, whose context is rebuilt from the record.

---

## 3. Qualification: one canary per route

A route may serve a tier only after it passes a **canary**: a small fixed set of the hive's own past tasks with known-good outcomes, re-run on that route. A new hive without history starts from a small shared starter set.

- **Statuses:** `candidate` → `qualified` → `retired`.
- **Every canary records its result and a one-paragraph lesson**, e.g. "good at bounded edits, loses track of multi-file refactors". The lesson feeds back into tier assignments.
- **Qualification is bound to the route pin.** A qualification records the pin it was earned on; `decide()` counts it only while the route's current pin matches. Editing a route in the table therefore voids its qualification by construction.
- **Qualification expires** when the model underneath a route changes without the table changing. The router detects this by comparing the model id each response reports with the pinned id; a mismatch triggers `route.drift_detected` and moves the route back to `candidate`.
- **Canaries are cheap by design:** a handful of tasks per route, rerun only on a change.

**Implementation.** `hive-route canary run TABLE ROUTE --suite DIR --sources FILE --log LOG` runs a suite (`schemas/suite-v1.schema.json`) on one route: each case copies its starting files into a scratch folder, starts the route's harness with the command in the sources file's `harnesses` section, then runs the case's **check from the suite directory** against the folder, so the attempt can't pass by editing its tests. The result (status, route pin, suite pin, per-case results, lesson) goes to the qualifications file, which the state reads (§7), and to the log as `route.canary_recorded`; `canary lesson` replaces the lesson with an operator's. `--max-usage` stops before a case if the route's pool is past that share of a window, since canaries draw on the same pools as live work. `canaries/starter` is the shared starter suite: a bug fix, a function from a spec, a change across files, and two reviews (one change with a bug, one without), each with a check validated against a reference solution.

**Drift, as built.** A launcher writes an **attempt manifest** (`schemas/attempt-v1.schema.json`; `hive-route manifest` makes one from a decision) next to each attempt's output. `hive-route observe LOG MANIFESTS…` reads the models the provider reported for each attempt (Claude Code: the stream's assistant messages; Codex: the session files for the attempt's folder) and logs `route.drift_detected` once per attempt when one differs from the route's model, sending the route back to `candidate`. A harness's own auxiliary models (the sources file's `aux_models`, e.g. Codex's `codex-auto-review` approval reviewer) are not drift.

---

## 4. The decision

The router makes two decisions for each attempt, both deterministic:

1. **Which tier the attempt needs**, computed from facts about the task (§4.1–4.3).
2. **Which route serves that tier**, given capacity and fit (§4.4).

`decide(request, table, state, mode) → decision` is a pure function (§4.7). The request carries the task facts and the attempt history; the state carries pool usage, route qualifications and the time `as_of`. The function reads no clock and no files, so the same inputs always give the same answer and every decision can be replayed. The tier is **recomputed at every attempt**: it drops when a plan or an independent check is added, and rises when attempts fail.

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

A fact that isn't supplied is `unknown`. Unknown facts take the table's conservative default (the costlier value, overridable per fact in `fact_defaults`) unless the scorer fills them. An unknown `leverage` counts as the threshold; an unknown author tier (F7) counts as `strong`. Each decision records every fact with its source, `supplied` or `default`.

**What the record supplies today.** The record's `1-hive` profile has only `kind` ∈ {`work`, `review`} on `task.created`, and no `specification`, `verification`, `scope` or `consequence`; task dependencies (for `leverage`) are deferred in the 1-hive plan. So in 1-hive every fact but `kind` is unknown until the profile carries them (§9.1) or the scorer is live, and the conservative defaults put almost every attempt in `strong`. That is correct, but it means `live` mode saves nothing over `fixed` until the facts exist.

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
| F8 | two or more `failed_check` attempts | one tier above the latest failed attempt's tier (§5); at `strong`, a `consult` is suggested |

Rule parameters and the kind defaults live in the route table, so a hive can tune them. Each decision records which rules applied.

**The creator's hint can only raise the tier.** A task's creator may ask for a higher tier, with a one-line reason. The way to get a *cheaper* tier is to change the facts: add an independent check, write the plan first, or split the task. That puts the effort where it pays: work that is well specified and independently checked is what cheap models do well. Only an operator override can go below the computed tier.

### 4.3 The scorer: filling unknown facts

Facts are often missing, especially `scope` and `specification` for tasks written in prose. The **scorer** is an optional plug-in that estimates them.

- **Input:** the task text, its pinned references and its history. **Output:** estimates for the unknown facts, each with a short reason, plus a suggested tier for comparison. Fixed schema; invalid output is discarded and the facts stay unknown.
- **It runs on one fixed route** (a local or light model), with capped output and a timeout. It is never routed itself, and its usage is counted like any other attempt.
- **It fills unknown facts only.** It never overrides a fact supplied by the task or the record, and it never sets the tier directly: its estimates go through the same rules as every other fact.
- **Shadow first.** It starts in shadow mode: its estimates are logged (`route.scored`) but not used. It goes live when replay (R9) shows that its estimates predict outcomes better than the conservative defaults.
- **It is called only when needed:** when a fact that could change the tier is unknown. Tasks with complete facts never call it.

**As built.** `decide --task-text FILE` runs the table's `scorer` when the tier with the unknown facts at their conservative values differs from the tier with them at their cheapest. The scorer route is called directly (harness `ollama`: the chat API with a JSON schema, temperature 0); only the unknown facts are asked for, and only well-formed answers are kept. `route.scored` logs the estimates with the tier with and without them, in both modes. In `live` scorer mode the estimates enter the request as `estimated_facts` (source `estimated`), so the decision replays exactly.

### 4.4 Choosing the route

**Rules, in order.** Each decision records the ids of the rules that decided it.

| # | Rule |
|---|---|
| RT1 | **Override first.** An operator override, recorded as an event, wins. |
| RT2 | **Qualified and fitting only.** Eligible routes are `qualified` at their current pin; their pool is not at a limit and is allowed for the project; for metered pools, the attempt's maximum cost fits what remains of every budget; their context limit fits the request with margin (`context_margin`, default 0.8); and they support tools if tools are needed (the default). An unknown capability, an unknown price, an unknown maximum cost or unknown usage counts as not fitting. The attempt's maximum cost is the smaller of the pool's per-attempt limit and the request's `max_attempt_usd`. |
| RT3 | **The computed tier is a floor.** Never go below it to save capacity. If every route in the tier is ruled out only by things that clear with time (a limit, a budget, an outage, a reassignment), return `wait` with the earliest known time a pool frees up (or none); the caller asks again then. If a tier in `allow_upgrade_on_wait` would wait, the next tier up is tried once. If no route in the tier could ever fit (none qualified, none fits), return `no_route`: that is a table problem for the operator, and it is never upgraded. The hive's supervisor or operator decides whether to escalate. |
| RT4 | **Reviews use a different model family** from the author's attempt. |
| RT5 | **Failures by class.** After a failed attempt the router acts on its failure class (§5), not on the fact that it failed. A request with `reason: reassign` excludes the previous route. |
| RT6 | **Choose within the tier by the table's preference.** Skip pools whose known usage is above the soft threshold (default 80% of any limit) while others are available; unknown usage is not "above". Then order by the table's `prefer` setting: `order` (listed order; the default), `cost` (subscription and local routes first, as free; then metered routes by expected cost, from the request's token `estimate` and the route's price, or else the attempt's maximum cost), or `headroom` (most remaining capacity first, unknown last; spreads load across pools). Ties keep the listed order. |
| RT7 | **Stay put when it works.** After an `interrupted`, `truncated`, `missing_info` or reconciled `indeterminate` attempt, the previous route is kept if it is still eligible and in the computed tier. |

### 4.5 Patterns the rules produce

- **Plan strong, execute light.** A `plan` task produces an explicit plan and acceptance checks. The `work` tasks that implement it are then `explicit` and `independent`, so unless their scope is large they run `light`. A `light` worker that finds the plan wrong reports that through the record (blocked, needs a decision) rather than improvising on a stronger model.
- **Consultations.** A `consult` task asks a strong model one question: the decision, the alternatives, the evidence, and what a useful answer looks like. The answer is a decision or a plan with the conditions under which it becomes invalid. It needs no tools and does none of the work. Equivalent questions from different tasks are merged into one consultation. This is the cheapest way to buy strong judgment.

### 4.6 Learning from outcomes

The record holds every attempt's facts, tier, route and outcome. Replay (R9) computes success rates and usage per kind, fact profile and tier, and proposes changes to the table's rules and defaults, e.g. "`work` with `scope: few` and `verification: weak` succeeds on `light` 90% of the time; drop F4 for it". A person approves each change as a new pinned table version, so routing never drifts on its own.

**As built, until R9 exists.** `hive-route report LOG --events FILE --agentsview ROOT` joins the log's attempts with task outcomes from the record's events (reviews passed and failed, closes) and output tokens per task folder from AgentsView, grouped by kind, facts, tier, route and mode; it tallies the scorer's estimated tiers against first-pass acceptance, and proposes trying a tier lower where a group of at least three tasks was always accepted without a failed review. A proposal is tried as a shadow table and checked with `whatif` before anyone pins it.

### 4.7 The `decide()` contract

Implemented in `src/hiveroute/decide.py`; the schemas are `schemas/request-v1.schema.json` and `schemas/state-v1.schema.json`.

- **Request:** `task`, `attempt` (the attempt id), optional `project`, `reason` (`new`, `restart`, `reassign`), `facts` (`kind` required), `author` {`family`, `tier`?} (required for reviews), `hint` {`tier`, `reason`}, `override` {`route_id`, `reason`, `by`}, `context_tokens`, `tools_needed`, `estimate` {`input_tokens`, `output_tokens`}, `max_attempt_usd`, `task_usage` (per pool, for per-task limits), and `history`: the task's earlier attempts, oldest first, each with `route_id`, `tier`, `class`, and for `capacity` optionally `limited_until`, for `indeterminate` `reconciled`.
- **State:** `as_of` (the decision's time), `pools` (usage, §7) and `routes` ({`status`, `route_pin`} per route, from canaries).
- **Mode:** `live` or `fixed` (§6).
- **Decision:** `decision` ∈ {`route`, `wait`, `no_route`, `reconcile`}; `tier` (what the attempt runs at) and `computed_tier` (what the rules require; they differ under an override, in `fixed` mode or after an RT3 upgrade); `route_id`, `route_pin`, `pool`, `model`, and the route's `harness`, `endpoint` and `effort` (what a launcher needs); every fact with its source; `reasons[]` (each rule that applied, with a note, and for tier rules the tier it required); `suggestions[]` (split the task, consult); `rejected` (each route ruled out, and why); `wait_until`; and the table pin.
- **Order of evaluation:** tier rules F1–F8 and the hint; RT1 override; an unreconciled `indeterminate` previous attempt returns `reconcile` (unless the table sets `indeterminate_retry`); `fixed` mode; then RT2–RT7 for the computed tier, and the next tier if RT3 allows it.

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
| `indeterminate` | timeout or crash after the call was sent, so it's unknown whether the provider ran it | Reconcile first, using the attempt id as the provider-side idempotency key where the provider supports one. Retry with a new attempt id only if the table allows the risk of paying twice (`indeterminate_retry`). Never record it as `failed`. |
| `interrupted` | the agent's process was lost or restarted for reasons that aren't the model's: killed, host restart, a supervisor restart after silence or a nudge | Same route (RT7). Not a reason to move route or tier. |

Every attempt has a stable **attempt id**. It is passed downstream as the idempotency key wherever the gateway or provider supports one, so a retried request doesn't become a second paid call.

The restart context passed to the next attempt includes each earlier attempt's route and failure class, so the next model doesn't repeat the same failure.

---

## 6. Modes

| Mode | Behavior |
|---|---|
| `fixed` | Every kind maps to one route (the table's `fixed` mapping). Qualification isn't required, since this is what runs before routing exists, but pool limits still apply, so a limited pool gives `wait`. Each decision still records the tier the rules would have computed (`computed_tier`), which is what the baseline is compared on. This is the baseline to measure against before routing is switched on. |
| `live` | `decide()` picks routes. |
| `shadow` | A second table decides alongside the live one; its choices are logged, not used. |
| `rollback` | Re-pin a previous table version. Every table change is a pinned version, so rolling back is one recorded step. |

Operator overrides (RT1) work in every mode.

**As built.** `hive-route mode MODE TABLE --log LOG` records a mode change; `decide` without `--mode` uses the log's latest. A shadow table is `decide --shadow-table FILE`: decided in `live` mode with the same request and state, logged as `route.shadow_decided`, replayed like any decision, never acted on. `hive-route whatif LOG TABLE` re-decides the whole log under another table, the check before pinning a change; rolling back is re-pinning the previous table file.

---

## 7. Usage tracking

The router needs each pool's usage against its limits. By pool kind:

- **Metered:** gateway logs give tokens and cost per request, tagged with actor, task and route. RT2 checks an attempt's maximum cost against the remaining budget; the gateway's budgets are the hard stop. Gateway figures are reconciled against provider invoices, and an estimate is never recorded as a settled charge.
- **Subscription:** the providers report the share of each window used, and the harnesses write those reports into their output: Claude Code's stream-json output (`rate_limit_event`: utilization and reset time per window, `status: rejected` at a limit) and Codex's session files (`token_count` events with `rate_limits`). The router reads the latest report (`hive-route state`, below). A report is `measured` while fresh (the sources file's `fresh_minutes`, default 30) and `estimated` after that, since usage can only have grown; once its window has reset it is `unknown`. A limit response sets `limited_until`. AgentsView, or the gateway's logs where there is one, gives tokens and cost per session, for attributing usage to tasks and routes.
- **Local:** queue length and concurrent requests.

Every usage figure carries `usage_basis`: `measured` (from gateway logs, or a fresh provider report), `estimated` (e.g. subscription usage from session files), or `unknown`. An `unknown` figure is never replaced with zero. For every kind, usage covers all attempts, including failed, indeterminate ones, reviews and consultations.

**State shape.** For each pool, `limited_until` (set from a provider's limit response or a `capacity` failure), `in_flight` (local pools), and `usage` keyed by limit: `usd/<per>` and `tokens/<per>` for metered pools (in USD or tokens), `window/<w>` for subscription windows (as a share of the window, 0 to 1, since the amounts are unknown), each with `used`, `basis` and optionally `resets_at`. A limit is at its end when `used` reaches its amount; a metered budget also blocks an attempt whose maximum cost exceeds what remains.

**Where the state comes from.** `hive-route state TABLE SOURCES` builds it, and `hive-route decide --sources SOURCES` builds it for each decision. The sources file (`schemas/sources-v1.schema.json`) is deployment data: per pool, the readers and paths to read (`claude-stream`, `codex-sessions`), `fresh_minutes`, and the qualifications file the canary runner keeps (§3). The built state is logged with each decision, so replay needs nothing else.

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

**As built.** `hive-route gateway-config TABLE` writes the LiteLLM config: one alias per route named by its `route_id`, calling the route's `gateway_model` (Ollama routes derive `ollama_chat/<model>`) with the pool's key from `gateway_key_env`; tags `pool:`, `tier:` and `route:` for spend per pool and route; no retries or fallbacks; `drop_params: false`; `store_model_in_db: false`. Subscription routes only with `--include-subscription`. Budgets aren't generated: the command lists what each metered pool needs, to be set up once the pinned gateway's enforcement is tested. Checked against LiteLLM 1.103.0: the config loads, the `qwen-local` alias reaches Ollama, and an unsupported parameter fails instead of being dropped.

**Routes through the gateway (`via_gateway`).** A route whose backend a harness can't call directly sets `via_gateway: true`: the harness asks the gateway for the route's alias (its `route_id`) and the gateway calls `gateway_model` at the route's `endpoint`. The common case is a self-hosted OpenAI-compatible server that speaks only chat completions: Codex now requires the Responses API, and Claude Code the Anthropic Messages API, and the gateway translates both. Lessons from putting 1-hive's SingularityCompute models behind LiteLLM 1.103.0:
- Use the `custom_openai/` provider prefix. With `openai/` or `hosted_vllm/`, LiteLLM forwards Responses and Messages requests to the backend's own `/v1/responses`, which such a server lacks (404), and then cools the deployment down (429s).
- Each harness adds a field a chat-completions backend rejects (Codex `client_metadata`, Claude Code `safeguards`). The pool's `gateway_drop_params` drops exactly those, by name; `drop_params` stays off.
- The harness reports the model as the alias, so drift checks accept the route id for these routes; a backend changing the model behind the gateway shows only in the gateway's logs.
- A sources `http-health` reader marks the pool at a limit while its endpoint is down, so the tier falls back before an attempt fails. (LiteLLM reserves `model_info.tier`, and skips a deployment that sets it with only a logged error, so route fields carry a `route_` prefix there.)

---

## 9. How it fits the hive

| Release | Integration |
|---|---|
| **R1 record** | The router is an actor of class `instrument` with its own key. It writes events in the reserved `route.` family (below) once the record admits them (§9.1). Until then it writes the same events to a local JSONL log that can be imported later. |
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

### 9.1 Routing on the record: amendment A1

The record froze as SPEC v1.0 on 2026-09-28 with the `route.` prefix reserved, so routing entered it as a numbered amendment: **hive-record A1** (2026-09-29, SPEC Appendix D; core legality table 1.1.0; tag `spec-v1.0-a1`). It admits six event types on the `hive` entity, with no refs, conditions or effects, so routing never touches task state: `route.table_pinned`, `route.mode_set` (class `instrument` or `operator`), `route.decided`, `route.waiting`, `route.canary_recorded`, `route.drift_detected` (class `instrument`). Task ids are in `data`, not the envelope.

**Summaries, bound to the full entry.** A decision replays only with its full request, state and table, which exceed the record's 8 KiB data limit. So the record gets a compact summary (the route, model, tiers, facts with which were estimated, and the ids of the rules that applied), and `log: {seq, digest}`: the entry's seq in the router's log and the SHA-256 of its canonical line. `route.scored` and `route.shadow_decided` stay in the router's log; they are evaluation data.

**Recording.** `hive-route record LOG` posts the summaries of entries not yet recorded through `hive emit`, as the router's actor (class `instrument`, its own key), with an idempotency key naming the entry's content; progress is kept in `<log>.recorded`. The summaries of 1-hive's real log were checked against hive-record's engine: all accepted.

**Not in A1, deferred:** facts as fields of `task.created` in the `1-hive` profile (today a launcher passes them; trigger: the chief of staff sets them on tasks through the record), usage fields on the profile's `cost` ext (trigger: a metered pool, where usage is measured), and re-declaring actors' `model_route` as route ids (trigger: an actor pinned to one route).

**The JSONL log** (`src/hiveroute/log.py`) is the interim form. Each line is `{v, seq, type, at, router, data}` in canonical JSON: `seq` is gapless (appends take a file lock), `at` is wall time and informational, `router` is the router's version. `route.decided` (and `route.waiting`, for every other outcome) carries the full request, state, mode and table pin with the decision; every table a decision names was logged in full by an earlier `route.table_pinned`. So `hive-route replay` recomputes every decision from the log alone, and an import into the record needs nothing else.

---

## 10. Route table example

A hive mixing API keys, a subscription and a local GPU (the same table, with placeholder model ids, is `fixtures/tables/mixed.yaml`; 1-hive's own is `examples/1-hive.yaml`):

```yaml
format: hive-route.table/1
version: 1
soft_threshold: 0.8
prefer: cost
kind_floor: {digest: local, work: light, review: light, plan: standard, triage: standard, consult: strong}
leverage_threshold: 3
unknown_facts: conservative      # the scorer fills them once it is live
context_margin: 0.8
scorer: {route: qwen-local, mode: shadow, max_output_tokens: 800, timeout_s: 20}
pools:
  anthropic-api: {kind: metered, limits: [{usd: 500, per: month}, {usd: 20, per: attempt}]}
  openai-api:    {kind: metered, limits: [{usd: 300, per: month}, {usd: 20, per: attempt}]}
  claude-plan:   {kind: subscription, limits: [{window: 5h}, {window: 7d}]}   # amounts unknown; the provider signals them
  local-gpu:     {kind: local, limits: [{concurrent: 2}]}
routes:                          # tools and context_limit must be declared: unknown counts as not fitting
  opus-api:      {tier: strong,   pool: anthropic-api, family: claude, model: <exact id>, effort: high, tools: true, context_limit: <tokens>, price: {in: <usd/Mtok>, out: <usd/Mtok>}}
  opus-plan:     {tier: strong,   pool: claude-plan,   family: claude, model: <exact id>, effort: high, tools: true, context_limit: <tokens>}
  gpt-high-api:  {tier: strong,   pool: openai-api,    family: gpt, model: <exact id>, effort: high, tools: true, context_limit: <tokens>, price: {in: <usd/Mtok>, out: <usd/Mtok>}}
  gpt-api:       {tier: standard, pool: openai-api,    family: gpt, model: <exact id>, effort: medium, tools: true, context_limit: <tokens>, price: {in: <usd/Mtok>, out: <usd/Mtok>}}
  sonnet-plan:   {tier: standard, pool: claude-plan,   family: claude, model: <exact id>, tools: true, context_limit: <tokens>}
  mini-api:      {tier: light,    pool: openai-api,    family: gpt, model: <exact id>, tools: true, context_limit: <tokens>, price: {in: <usd/Mtok>, out: <usd/Mtok>}}
  qwen-local:    {tier: local,    pool: local-gpu,     family: qwen, model: <exact id>, tools: true, context_limit: <tokens>}
tiers:
  strong:   [opus-plan, opus-api, gpt-high-api]   # subscription first; API when it hits its window
  standard: [sonnet-plan, gpt-api]
  light:    [mini-api]
  local:    [qwen-local]
allow_upgrade_on_wait: [local, light]  # may run one tier up rather than wait
fixed: {work: opus-plan, review: gpt-high-api}   # the baseline (§6)
```

A hive on API keys only lists metered pools and sets `prefer: cost`. A hive on subscriptions only lists subscription pools and may set `prefer: headroom`.

---

## 11. Build sequence

Every step is built (2026-09-29); what's left is operational (§11.1).

1. **Core library and CLI:** `hiveroute`, `hive-route check|decide|mode|replay`; rule fixtures; JSONL log.
2. **Gateway:** `hive-route gateway-config` (§8). Per-actor keys and budgets wait for a metered pool: they are gateway setup, tested on the pinned version.
3. **Usage tracking:** `hive-route state`, `decide --sources` (§7). Subscriptions from providers' reports; metered pools from the gateway once there is one; local pools as concurrency.
4. **Launcher integration:** 1-hive's launcher (§11.1).
5. **Canaries and drift detection:** `hive-route canary run|lesson`, `canaries/starter`, attempt manifests, `hive-route observe` (§3).
6. **Scorer:** `decide --task-text`, on a local model, in shadow mode (§4.3).
7. **Record events:** hive-record A1 and `hive-route record` (§9.1).
8. **Shadow tables and tuning:** `decide --shadow-table`, `whatif`, `report` (§4.6, §6).

### 11.1 Where 1-hive stands (2026-09-30)

**Live since 2026-09-30** (route log seq 38; routing recorded on the record through A1, as the `router` instrument).

**Pools:** a Claude Pro plan (Claude Code), a ChatGPT plan (Codex), SingularityCompute (self-hosted, through the hive gateway: LiteLLM 1.103.0, user service `hive-llm-gateway` on 127.0.0.1:4000, `1-hive/deploy/gateway-up.sh`) and the host's GPU (Ollama, `qwen3:8b`, for the scorer in shadow mode). Table `1-hive/deploy/route-table.yaml` v7 (strong: Opus high, then gpt-6-astra high; standard: DeepSeek on SingularityCompute, Opus medium, Sonnet, gpt-5.6-sol medium; light: Qwen on SingularityCompute, Sonnet low, gpt-5.6-sol low); SingularityCompute is listed first in its tiers (`prefer: order`), backed up by the plans.

**Qualifications** (starter suite): every route in the table passed 5/5 except sc-deepseek-v4, which passed 4/5 (it accepted a trailing tab in the strict input-validation case) and was accepted by the operator (`canary accept`): self-hosted, fast, cheap to retry. sc-gpt-oss-120b and sc-minimax-m3 also passed 4/5 and were removed rather than accepted. haiku-plan failed 5/5: Claude Code's auto permission mode refuses its edits. Qwen is slow (5–8 minutes per small task); DeepSeek and gpt-oss finish the suite in about two minutes. Codex can't drive chat-completions models here (its `apply_patch` is freeform-only), so SingularityCompute routes run on Claude Code.

**Effect:** tasks without facts still route strong (Opus first); the chief of staff lowers a task's tier by supplying facts (`ROUTE_FACTS` at launch), which sends light work to SingularityCompute first.

**Next:** gather outcomes and tune with `report` and `whatif`; decide whether self-hosted routes may qualify at 4/5 (a failed attempt there costs little and an independent check catches it); turn the scorer live once replay supports it.

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
