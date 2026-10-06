# Adopting hive-route

How a hive puts its agents' model choice behind hive-route. [`ROUTING.md`](../ROUTING.md) is the design; this is the path to running it. 1-hive's deployment is the worked example throughout: [`1-hive/deploy`](https://github.com/1-hive/1-hive/tree/main/deploy) and [`tools/launch-task.sh`](https://github.com/1-hive/1-hive/blob/main/tools/launch-task.sh).

Linux only. You need Python ≥ 3.10 (Ubuntu 22.04's default is enough), `jq`, and the harnesses your agents run (Claude Code, Codex, OpenClaw). A gateway (§5) also needs rootless Podman; a local model, Ollama.

```sh
# with uv
git clone https://github.com/1-hive/hive-route && cd hive-route && uv sync
uv tool install -e .          # puts `hive-route` on your PATH

# or with the system Python, no uv
python3 -m venv ~/.local/share/hive-route && ~/.local/share/hive-route/bin/pip install \
  "git+https://github.com/1-hive/hive-route"
ln -s ~/.local/share/hive-route/bin/hive-route ~/.local/bin/hive-route
```

(On Debian and Ubuntu, `python3 -m venv` needs the `python3-venv` package.)

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
  - `http-health`: a URL that must answer 2xx, else the pool is treated as at a limit for `down_minutes`, so its tier falls back. Give API-key pools one too, against the provider's free model list with the key in the provider's header (`auth_header: x-api-key`, `headers: {anthropic-version: "2023-06-01"}` for Anthropic): a revoked or expired key then stops routing instead of failing attempts.

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
   - `history`: earlier attempts with their failure `class` (`outage`, `capacity`, `truncated`, `missing_info`, `failed_check`, `stalled`, `indeterminate`, `interrupted`, `checkpoint`). A second `failed_check` moves one tier up. A `checkpoint` is a worker asking to be routed again at a milestone (optional; see Checkpoints below).
2. **Decide:** `hive-route decide TABLE - --sources SOURCES --log LOG [--task-text KICKOFF]`. Exit 0 means route; exit 3 means `wait` (retry at `wait_until`), `no_route` (the table can't serve it; an operator's problem) or `reconcile` (settle an attempt whose outcome is unknown first). Never start anything on exit 3.
3. **Start** the decision's `harness` with its `model` and `effort`; through the gateway when `via_gateway` is set (§5).
4. **Write the manifest:** `hive-route manifest DECISION --cwd DIR --output FILE` into your state folder, and run `hive-route observe LOG 'STATE/*.attempt.json'` before each decision: it compares the models the provider reported with each route's and flags drift.

Two rules from 1-hive's security review:
- **Validate ids before using them.** Task and attempt ids end up in file paths, a Codex `-c` config override and an HTTP header; a quote, newline or `../` in one can rewrite the harness's provider, inject a header or pick another key file. Accept only the record's id syntax, `^[a-z0-9][a-z0-9._-]{0,63}$` (the example launcher does).
- **Keep the log, manifests and history outside the agents' working folders**, so an agent doesn't edit what steers the router (failure history moves tiers; manifests drive drift checks).

### Facts: what lets routing save

A fact the request doesn't carry takes its costliest value, so **a task without facts runs on your strongest tier.** Routing in live mode saves nothing until the agents that write tasks also state their facts. Ask them to; it's cheap, since whoever writes a task already knows the answers:

- `specification`: does the task pin a plan or interface and an acceptance check (`explicit`), only part of it (`partial`), or only the goal (`goal_only`)?
- `verification`: will a check the worker can't edit decide whether it's done: protected tests, a reference result, an independent reviewer with a stated check (`independent`)? Only the worker's own tests (`weak`)? Nothing (`none`)?
- `scope`: how many components it touches (`single`, `few`, `many`).
- `consequence`: `costly` if it spends external compute, touches shared or production state, or is hard to undo; otherwise `reversible`.
- `leverage`: how many tasks wait on this one.

What worked in 1-hive:
- **Put the facts in the task, not the launch.** The agent that writes an order adds one line, e.g. `Route facts: specification=explicit verification=independent scope=few consequence=reversible leverage=0`, and the launcher reads it. The facts then sit next to the order they describe, and restarts and reviews reuse them.
- **Allow `unknown`, refuse silence.** A fact can be `unknown` (it then takes the costly default), but a new task with no facts line is refused at launch. That catches forgotten facts without forcing guesses.
- **Tell the writer how facts lower the tier:** a plan pinned and an independent check make `light` work possible (ROUTING.md §4.5). Writing the plan first or adding the check is usually worth more than arguing over the tier.
- **Over-optimistic facts are bounded.** If a cheap attempt fails its checks twice, the tier goes up (F8). The cost is a wasted cheap attempt, as long as results are checked independently.

A hive without such an agent can still use the scorer (ROUTING.md §4.3) to estimate unknown facts from the task's text, once replay shows its estimates are good.

### Checkpoints (optional)

A worker can end its attempt at a milestone and ask to be routed again (ROUTING.md §4.5): down after it has written a plan, up when the task needs more. Your launcher passes the next attempt's `history` with class `checkpoint`, and the facts (or a hint) as they now stand. You don't need it: a launcher that never sends `checkpoint` behaves as before. To adopt it, give your workers a way to ask (1-hive: a `route-checkpoint.json` and a checkpoint report on the record; see its [worker contract](https://github.com/1-hive/1-hive/blob/main/docs/worker-contract.md)), and have the component that restarts them check the request: only `specification` and `scope` should change on a worker's word, a request up becomes a `hint`, and a task gets a few at most. 1-hive's [`supervisor.py`](https://github.com/1-hive/1-hive/blob/main/tools/supervisor.py) does this.

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
- **Label the runtime.** Put `runtime` in each request (`host`, `container`, …). The router doesn't use it to decide, but the report keeps evidence from different runtimes apart: a change of runtime (sandboxing, OS user, permission mode) can change outcomes as much as a change of model. After one, run the canaries again: the runtime isn't part of a route's pin, so nothing voids qualifications by itself.
- `hive-route whatif LOG TABLE2`: the whole log re-decided under a proposed table, before you pin it.
- `decide --shadow-table TABLE2`: a second table decides alongside, logged and never acted on.
- The scorer (`scorer:` in the table, a local model) estimates facts a task didn't supply. It starts in shadow mode; make it live only once the report shows its estimates match outcomes.

## 9. Long-running OpenClaw agents

If your agents run continuously and take their model from an OpenClaw config (chat bots, Iter on an OpenClaw backend), there's no launcher to put the router in front of. Bind them instead (ROUTING.md §9.3). Everything below has a worked example: [`fixtures/tables/openclaw.yaml`](../fixtures/tables/openclaw.yaml) (a table), [`examples/openclaw-bindings.yaml`](../examples/openclaw-bindings.yaml), [`examples/openclaw-sources.yaml`](../examples/openclaw-sources.yaml) and [`examples/openclaw-bind.sh`](../examples/openclaw-bind.sh). Keep your own copies of these with your deployment, not in this repository: they describe your infrastructure.

**Start with one agent.** Bind a single agent first and take it through every step below; add the others once it has run live for a while. A problem then stays with one agent, and its before and after are easy to compare.

1. **Routes:** one per model your agents may use, with `harness: openclaw` and the model as OpenClaw names it, `provider/model` (e.g. `anthropic/claude-opus-5-5`), in a pool per subscription, API key or local server. Pick each pool's kind by how it's limited: `subscription` for usage windows, `metered` for money (pay per use), `local` for capacity (your own server, or a flat-rate endpoint limited by concurrency). If several agents share one subscription, they share one pool, and every agent's session database belongs in its reader.
2. **Bindings:** one per agent whose model you want routed: the OpenClaw config file, `target` (`defaults` for `agents.defaults`, or the agent's id for `agents.entries.<id>`), the agent's `kind` and facts, `sessions` (its `openclaw-agent.sqlite`), and, for fixed mode, `route` and `fallbacks` set to what it runs today. Use the config paths as the `openclaw` command sees them: run the bind script where the router and OpenClaw see the same files (on the host with the state bind-mounted at the same path, or inside the agents' container).
3. **Sources:**
   - an `openclaw-sessions` reader for every pool, over the agents' session databases (it reads them read-only; compressed events need `zstd`, the binary or the `zstandard` module);
   - for a Claude subscription, a usage probe (ROUTING.md §7): a one-line `claude -p` call whose output gives the 5-hour and 7-day shares, run hourly at most and only after the agents were active; a `claude-stream` reader over its output folder. The probe must use **the agents' subscription**, not the login of whoever runs the router: if the agents use a setup-token, give it to the probe with `env_from: [CLAUDE_CODE_OAUTH_TOKEN]` and a root-only `env_file` (with an `env_file`, the token comes from that file only, never from the environment of whoever runs the router), or run the probe where the agents' login is. Without a probe the router only learns that the pool is at a limit, after a rate-limit error;
   - for GPT through Codex's app-server, a `codex-sessions` reader over the agent's `codex-home/sessions`;
   - the `openclaw` harness, for canaries (`openclaw agent exec --json`). If the agents run in a container, run the canary there as the agent's user (`docker exec -u …`), with a `canary_workdir` both sides can write, and `path_map` from the host's path to the container's.
4. **Fixed mode first:** `hive-route mode fixed TABLE --log LOG`, then `hive-route openclaw-config TABLE BINDINGS --sources SOURCES --check`. It changes nothing: it prints each binding's model chain and the patch per config file, and `--check` says for each binding whether it **matches the config** or **would change** it (and how: the model chain, and any key the patch would add, such as a `models` map the agent's entry doesn't have yet), and exits 8 if a model isn't in the config's `modelPolicy.allow` list or its provider isn't configured. When every binding matches what the agent runs today, run `examples/openclaw-bind.sh` from a timer (e.g. every 5 minutes): the agents' models are now declared and logged, and drift is checked.
5. **Qualify, then go live:** after a few days in fixed mode, `canary run` for each route through the `openclaw` harness (§6), then, with your operator's approval, `hive-route mode live TABLE --log LOG`. From then on the bind script moves agents off a pool at its limit, and back when it frees up.

**Feedback.** If a step didn't fit your hive or the docs were unclear, open an issue on this repository: the next hive adopts from the same docs.

**Applying patches.** `openclaw-bind.sh` applies a changed patch with `openclaw config patch`, after OpenClaw's own dry run validates the result; OpenClaw hot-applies model settings without a restart. If your hive changes agent config only through a controller, a review step or a checksum gate, replace its `apply()` with a call to that: the patch is a plain JSON merge patch (named per config file in the output's `patch_files`), and the router never writes a config itself. If several bound agents share one config file and your controller applies changes per agent, write one patch per binding instead (`--write-dir DIR --per-binding`: `DIR/<binding>.patch.json`, also in the output's `binding_patches`). **Live mode needs a working apply step**: a controller that can't change agent config must gain that action first (reviewed like any other), or live mode can only queue changes.

**Unread is not idle.** If a session database can't be read (permissions, a sandbox, a missing `zstd`), the reader reports it in the pool's source (`hive-route state … --notes`), metered spend stays unknown (which blocks the pool), and `openclaw-observe` exits 10 rather than reporting no drift.

**What binding gives you, and facts for bound agents.** A binding routes per agent, not per request. Without facts each agent stays on its strongest tier, so live mode mostly keeps today's chain; what it adds is moving agents off a pool at its limit before their requests fail on it, and a record of which model each agent used, with drift alerts. Saving on tiers needs two things:
- **Facts true of the agent's role,** i.e. of its most demanding request, not of each one. For a low-stakes helper bot, `consequence: reversible`, `leverage: 0` and `verification: none` are usually honest. State `leverage`: an unknown one counts as the threshold, which keeps an agent without an independent check on `strong` (F6). With those facts and a kind other than `consult`, the floor is `standard`.
- **A cheaper route in that tier,** e.g. a smaller model on the same subscription, which uses less of its windows.

Per-request savings need per-request facts: an agent with its own loop (Iter) can ask the route service at each episode start (§10) instead of being bound. A second pool for the strong tier (another subscription or an API key) is what lets the router spread load rather than only wait.

**Pick an active agent for the pilot.** An agent with little traffic gives little to measure: check its recent turns before choosing.

## 10. Agents with their own loop: routing per episode

For an agent that runs its own loop (Iter), the router can choose per episode instead of per agent: easy episodes go light, hard ones stay strong. It's the route service (ROUTING.md §9.2): `hive-route serve`. The agent needs a small hook; nothing else in its loop changes.

**Run the service**
1. An **agents file** ([`examples/agents.yaml`](../examples/agents.yaml)): per agent, the SHA-256 of its token (quoted), the `kinds` it may ask for, and its fixed `facts`, i.e. what's at stake in its role (`verification`, `consequence`, `leverage`; see §4, "Facts"). Keep it where the agents can't write.
2. `hive-route serve TABLE AGENTS --log LOG --state-dir STATE --sources SOURCES` (default `127.0.0.1:8480`; `--host` and `--port` to change). `STATE` holds each agent's episodes; keep it out of the agents' reach too. Run it as a service; `GET /health` answers with the table pin and mode.
3. The same table, sources, canaries and modes as everything else: in `fixed` mode the service answers with the table's fixed route for the kind, which is the baseline.

**The hook in the agent** (the contract)
- **At each episode start:** `POST /route` with `Authorization: Bearer <token>` and
  `{"task": "<id>", "episode": "<id>", "facts": {"specification": ..., "scope": ...}}`.
  Ids match `^[a-z0-9][a-z0-9._-]{0,63}$`; an episode id is used once; `task` groups the episodes of one piece of work, so earlier failures count. Optional: `kind` (one of the agent's), `hint` (`{"tier", "reason"}`, raises only), `context_tokens`, `estimate`, `tools_needed`, and `text` (the request, for the scorer to estimate unknown facts). Leave a fact out when unsure: it takes the costly default.
- **The answer** is a decision (ROUTING.md §4.7). On `"decision": "route"`, run the episode on `model` (with `endpoint`, `effort`) and keep it until the episode ends, through every tool round trip. On `wait`, ask again at `wait_until` (or later if it's null); on `no_route` or `reconcile`, stop and tell the operator. Never pick a model yourself.
- **At each episode end:** `POST /episodes/end` with `{"episode": "<id>", "class": ...}` when it didn't simply succeed: `outage`, `capacity` (with `limited_until` if the provider said), `truncated`, `missing_info`, `failed_check`, `stalled`, `indeterminate`, `interrupted`, or `checkpoint` (the agent wants the rest routed again, e.g. after writing a plan). A success needs no call.
- **Judging the episode's facts.** `specification`: does the request pin what's wanted and how it's checked (`explicit`), partly (`partial`), or only a goal (`goal_only`)? `scope`: how much it touches (`single`, `few`, `many`). A small classifier step in the agent, or the router's scorer through `text`, can set them; the service refuses any other fact from the agent.
- **Bound or served, not both:** stop binding an agent (§9) before its hook goes live.

Start in `fixed` mode with one agent, as with bindings, and switch to `live` with your operator's approval once its routes are qualified.

## 11. Known limits

- The router steers by usage; the provider or gateway enforces the caps. Subscription usage between harness reports is an estimate.
- Agents that run as your own OS user can read your key files and the router's state; a separate OS user per agent is the real boundary.
- Tiers are only as good as the facts you give them: with no facts, every task routes strong.
- The route service doesn't yet check which model answered a served agent's episodes (drift); bindings and launched attempts are checked.
- OpenClaw doesn't record subscription window shares: without a usage probe, a bound agent's subscription pool is known only to be at a limit (after a rate-limit error), not how close it is.
