# Adopting hive-route

How a hive puts its agents' model choice behind hive-route. [`ROUTING.md`](../ROUTING.md) is the design; this is the path to running it. 1-hive's deployment is the worked example throughout: [`1-hive/deploy`](https://github.com/1-hive/1-hive/tree/main/deploy) and [`tools/launch-task.sh`](https://github.com/1-hive/1-hive/blob/main/tools/launch-task.sh).

Linux only. You need Python ≥ 3.11 with [uv](https://docs.astral.sh/uv/), `jq`, and the harnesses your agents run (Claude Code, Codex). A gateway (§5) also needs rootless Podman; a local model, Ollama.

```sh
git clone https://github.com/1-hive/hive-route && cd hive-route && uv sync
uv tool install -e .          # puts `hive-route` on your PATH
```

## 1. The order of work

1. Write a **route table** (§2) and a **sources file** (§3).
2. Put the router in front of your **launcher** (§4), in **fixed** mode: today's models, now explicit and logged. That's your baseline.
3. Qualify each route with **canaries** (§6).
4. Switch to **live**.
5. Measure and tune (§8).

Nothing changes what your agents run until step 4.

## 2. The route table

One YAML file (`schemas/table-v1.schema.json`); [`examples/1-hive.yaml`](../examples/1-hive.yaml) is a complete one.

- **Pools** are where capacity comes from, each with its limits:
  - `subscription`: usage windows, e.g. `[{window: 5h}, {window: 7d}]` (Claude, ChatGPT plans);
  - `metered`: money, e.g. `[{usd: 50, per: month}, {usd: 5, per: attempt}]` (API keys);
  - `local`: concurrency, e.g. `[{concurrent: 4}]` (your own models).
- **Routes** name one way to run a model: `tier`, `pool`, `family`, `harness` (`claude-code`, `codex`), `model`, and where it applies `effort`, `price` (metered: USD per million tokens in and out), `tools: true`. A route's pin is the digest of its definition, so any edit makes it a new route that needs qualifying again (§6).
- **Tiers** list routes in order of preference: `strong`, `standard`, `light`, `local`. The router never drops below the tier a task needs; later routes in the list are the backups.
- **`fixed`** maps each kind of task to one route: what your launcher runs today. Fixed mode uses it, and it is your baseline.
- `prefer`: `order` (the listed order), `headroom` (spread load across pools) or `cost`.

Check it with `hive-route check TABLE`.

## 3. The sources file

Deployment data the router reads at each decision (`schemas/sources-v1.schema.json`; 1-hive's [`route-sources.yaml`](https://github.com/1-hive/1-hive/blob/main/deploy/route-sources.yaml)).

- **`pools`:** how each pool's usage is read.
  - `claude-stream`: Claude Code's stream-json output files (a launcher writes them with `claude -p --output-format stream-json`); the provider's window shares.
  - `codex-sessions`: `~/.codex/sessions`; the provider's window shares.
  - `gateway-spend`: a metered pool's spend from the gateway (§5).
  - `http-health`: a URL that must answer 2xx, else the pool is treated as at a limit for `down_minutes`, so its tier falls back.

  A pool with no reader has unknown usage. For a metered pool, unknown usage blocks it: the router never guesses at money.
- **`qualifications`:** the file canaries write (§6).
- **`harnesses`:** how to start each harness for canaries: an argv template with `{model}`, `{prompt}`, `{cwd}`, `effort_args`, and for gateway routes `gateway_env` / `gateway_args`. `aux_models` lists models a harness uses for itself (Codex's `codex-auto-review`), which aren't drift.
- **`gateway`:** its URL and the key agents use (§5).

`hive-route state TABLE SOURCES --notes` prints what the router sees.

## 4. The launcher

Your launcher asks the router before each attempt and starts whatever it answers. [`examples/launch.sh`](../examples/launch.sh) is a small complete one; copy it and edit its settings block.

Per attempt:

1. **Build the request:** `task`, `attempt` (unique per attempt), `facts.kind` (`work`, `review`, …). Add what the task's creator knows, since facts are what lower the tier:
   - `facts`: `specification` (`explicit`/`partial`/`goal_only`), `verification` (`independent`/`weak`/`none`), `scope` (`single`/`few`/`many`), `consequence` (`reversible`/`costly`), `leverage`;
   - `author: {family, tier}` for a review (it must run on another family);
   - `history`: earlier attempts with their failure `class` (`outage`, `capacity`, `truncated`, `missing_info`, `failed_check`, `stalled`, `indeterminate`, `interrupted`). A second `failed_check` moves one tier up.
2. **Decide:** `hive-route decide TABLE - --sources SOURCES --log LOG [--task-text KICKOFF]`. Exit 0 means route; exit 3 means `wait` (retry at `wait_until`), `no_route` (the table can't serve it; an operator's problem) or `reconcile` (settle an attempt whose outcome is unknown first). Never start anything on exit 3.
3. **Start** the decision's `harness` with its `model` and `effort`; through the gateway when `via_gateway` is set (§5).
4. **Write the manifest:** `hive-route manifest DECISION --cwd DIR --output FILE` into your state folder, and run `hive-route observe LOG 'STATE/*.attempt.json'` before each decision: it compares the models the provider reported with each route's and flags drift.

Two rules from 1-hive's security review:
- **Validate ids before using them.** Task and attempt ids end up in file paths, a Codex `-c` config override and an HTTP header; a quote, newline or `../` in one can rewrite the harness's provider, inject a header or pick another key file. Accept only the record's id syntax, `^[a-z0-9][a-z0-9._-]{0,63}$` (the example launcher does).
- **Keep the log, manifests and history outside the agents' working folders**, so an agent doesn't edit what steers the router (failure history moves tiers; manifests drive drift checks).

## 5. The gateway (only when needed)

You need it for models your harnesses can't call directly (self-hosted OpenAI-compatible servers: Codex needs the Responses API and Claude Code the Anthropic API) and for pay-per-call API keys (spend, budgets, per-agent keys). Subscriptions go direct.

1. **Routes:** set `via_gateway: true` and `gateway_model` (the provider-prefixed model LiteLLM calls: `anthropic/claude-sonnet-5-5`; for a chat-completions-only server `custom_openai/<model>` with `endpoint`). A pool's API key comes from `gateway_key_env`.
2. **Config:** `hive-route gateway-config TABLE > litellm.yaml`: an alias per route, no retries or fallbacks, unsupported parameters fail. Never edit it; regenerate it.
3. **Run it:** LiteLLM with its own Postgres, loopback only, pinned version. 1-hive's [`gateway-up.sh`](https://github.com/1-hive/1-hive/blob/main/deploy/gateway-up.sh) does all of it: installs LiteLLM 1.103.0 as a uv tool with its Prisma client (without it the service crash-loops), starts the database, generates the config, creates a **worker key** that may only call the routes, and applies the budgets.
4. **Budgets:** `hive-route gateway-config TABLE --budgets` lists a tag budget per metered period (`pool:<id>:<period>`); `hive-route attempt-budget TABLE DECISION` gives the one a launcher creates before each metered attempt (`attempt:<id>`). Budgets stop spending once logged spend passes them; calls in flight can overshoot.
5. **Keys:** agents get the worker key only. The master key can create keys and change budgets; it stays with the launcher.

Lessons from 1-hive (LiteLLM 1.103.0):
- Use `custom_openai/` for chat-completions-only backends; `openai/` and `hosted_vllm/` forward to a `/v1/responses` they don't have.
- Codex and Claude Code each send a field such backends reject; list them in the pool's `gateway_drop_params` (`client_metadata`, `safeguards`).
- Codex 0.158 only offers `apply_patch` as a freeform tool, which doesn't survive the translation, so it can't edit files there: run those routes on Claude Code.
- LiteLLM reserves `model_info.tier`.
- **Claude Code's auto permission mode runs its safety check on the session's model.** Routed to a slow or weak self-hosted model, the checks time out (actions refused) and a weaker model judges what's safe. 1-hive paused its self-hosted routes for agent work until agents run in a container; run such routes where a per-action check isn't needed, or don't route agentic Claude Code sessions to them.

## 6. Qualifying routes, and going live

A route serves live traffic only once qualified at its current pin.

- `hive-route canary run TABLE ROUTE --suite canaries/starter --sources SOURCES --log LOG --max-usage 0.8`: five small tasks with checks the model can't edit. `--max-usage` stops before a case once the route's pool is past 80% of a limit, since canaries draw on the pools live work uses. On a metered route, `--case fix-bug` alone keeps the cost down.
- `canary lesson` records what you learned; `canary accept` (an operator) accepts a route below the bar, with a reason.
- Add cases from your own work to a suite of your own: the starter suite says a route works, not that it's good at your tasks.
- **Go live** with `hive-route mode live TABLE --log LOG` when every tier your tasks need has a qualified route in each family (reviews need another family). `mode fixed` goes back.

## 7. On a hive record

With [hive-record](https://github.com/1-hive/hive-record) at amendment A1 (tag `spec-v1.0-a1`), the router records its decisions: register an actor of class `instrument` with its own key, then run `hive-route record LOG` with that identity (`HIVE_URL`, `HIVE_ID`, `HIVE_KEY_FILE`) after each decision. It posts compact summaries bound to the log's full entries, once each, and skips entries the record would refuse. Sync only your real log, never a scratch one: a test run synced by mistake leaves its entries on an append-only record.

The record also requires an actor to re-declare when its configuration changes (SPEC §6.2). A routed actor's model can change at every attempt, so before an attempt whose harness or model differs from the actor's declaration, the launcher emits `actor.declared` with the actor's own key (`model_route`: `<route_id>: <model> (<effort>)`); 1-hive's launcher shows how.

## 8. Measuring and tuning

- `hive-route report LOG --events <(hive events) --agentsview ROOT`: attempts by kind, facts, tier and route, with outcomes from the record and tokens from AgentsView; it proposes a tier lower where a group was always accepted.
- `hive-route whatif LOG TABLE2`: the whole log re-decided under a proposed table, before you pin it.
- `decide --shadow-table TABLE2`: a second table decides alongside, logged and never acted on.
- The scorer (`scorer:` in the table, a local model) estimates facts a task didn't supply. It starts in shadow mode; make it live only once the report shows its estimates match outcomes.

## 9. Known limits

- The router steers by usage; the provider or gateway enforces the caps. Subscription usage between harness reports is an estimate.
- Agents that run as your own OS user can read your key files and the router's state; a separate OS user per agent is the real boundary.
- Tiers are only as good as the facts you give them: with no facts, every task routes strong.
