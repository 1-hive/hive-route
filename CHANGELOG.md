# Changelog

What changed in each revision of hive-route, and what a hive running it needs to do. Revisions follow `ROUTING.md`'s `rev`. **Adopters** says whether an existing deployment must change anything.

## rev 11 — 2026-10-07

- **Shadow scorers:** `shadow_scorers:` in the table runs more scorers next to `scorer`, always in shadow mode, each with its own `route.scored`; `report`'s scorer counts are per scorer route.
- **`systemone` harness** for decision models with TypeSafe's System One API (Jev, a self-hosted Kev): one `choice` per fact (verification as two questions: any check named, then who controls it), probabilities logged, optional `min_probability`. Short option labels for specification and scope, and no "choose the costlier option" sentence: Kev scores each option separately, so it can't use that. On `fixtures/scorer/suite.jsonl`, Kev-4B matches 76% of labels, with 5 of 59 tiers below the labels' tier. The first wording matched 61% (verification as one choice scored 17/59), with 17 tiers lower. Scorers take `max_chars` (default 12,000, as before). The gateway config skips `systemone` routes.
- **`hive-route scorer-eval`** runs scorers on a labelled suite: accuracy per fact and source, estimates cheaper than the label, tier agreement, and accuracy by probability. Suites in `fixtures/scorer`.

**Adopters:** nothing required. `report --json`'s `scorer` keys now start with the scorer's route.

## rev 10 — 2026-10-06

- The `openclaw-sessions` reader filters rows on `created_at` in SQL (with an hour's margin; its unit, seconds or milliseconds, read from the newest row) instead of reading and decompressing every event. The first pilot measured about 8 s and 330 MB per state build without it, too slow for the route service, where an agent's hook waits on it. Reported by the pilot.

**Adopters:** nothing required.

## rev 9 — 2026-10-06

From the first review of the route service by an adopter (Iter on an OpenClaw gateway).

- **Runner facts:** an agent may have a runner, a trusted component outside it with its own token (`runner_token_sha256`), which may state `runner_facts` per episode, e.g. `consequence` from the tools it grants. For general agents whose stakes vary per request.
- **`ignore_models`** per agent: names an episode report may hold that aren't models (a gateway's agent target, `openclaw/*`), never drift.
- **Hook contract** (ADOPTING.md §10): report the model that *served* each call, not a gateway's `model` field; end an episode at once on a rate limit or outage and route a new one; fail open to the configured chain when the service is down, logged locally, with no `/episodes/end`; binding the service for agents in containers.
- `openclaw-config` without `--mode` or a logged mode warns that it renders live mode; ADOPTING §9 step 4 passes `--log` (or `--mode fixed`).

**Adopters:** nothing required. Served agents that relied on a gateway's `model` field should report the served model instead, or list the gateway's names in `ignore_models`.

## rev 8 — 2026-10-06

- **The route service** (ROUTING.md §9.2, ADOPTING.md §10): `hive-route serve` answers `POST /route` at the start of each episode of an agent with its own loop (Iter), and takes `POST /episodes/end` with the episode's class. Facts at stake are fixed per agent in an agents file (`schemas/agents-v1.schema.json`, `examples/agents.yaml`); the agent states only `specification` and `scope` per episode. The service keeps each agent's episode history and logs every decision, which replays. Every episode's end reports the models the provider named; one that isn't the route's is drift (`route.drift_detected`, `source: agent report`) and demotes the route. Standard library only, no new dependency.
- `decide` and the service share one path for scoring, deciding and logging.
- Drift checks (bindings and the service) count only a **dated** snapshot as the same model (`claude-x-20261001`, `gpt-x-2026-09-30`); before, any suffix did, so `claude-x-mini` passed for `claude-x`.

**Adopters:** nothing required; a bound agent answering with an undated variant of its model is now drift. To route Iter agents per episode, add the hook in ADOPTING.md §10 and stop binding those agents.

## rev 7 — 2026-10-06

Fixes from the first OpenClaw adoption (Ben's proto-hive).

- `openclaw-config --check` reported "matches the config" when the patch would still add a `models` map (or an entry in it). It now lists every key the patch would add or set.
- `--write-dir DIR --per-binding` writes one patch per binding (`DIR/<binding>.patch.json`; `binding_patches` in the output), for controllers that apply changes per agent while several agents share one config file. The default stays one patch per config file.
- A usage probe with an `env_file` reads its `env_from` variables from that file only. Before, a variable of the same name in the router's own environment won, so a manual run could measure the wrong subscription.
- ADOPTING.md §9: what binding gives without facts, and role-level facts for bound agents.

**Adopters:** OpenClaw bindings: re-run `--check`; a binding that now says "would change … adds …models" was already being changed by its patch. If you set a probe token in the router's environment on purpose, move it to the `env_file` or drop `env_file`. Nothing else.

## rev 6 — 2026-10-06

- **Checkpoints** (ROUTING.md §4.5, §5): a worker can end its attempt at a milestone and ask to be routed again, down after a plan or up when the task needs more. New attempt-end class `checkpoint` in the request's `history`; RT7 keeps the route when the tier is unchanged.
- **Facts guidance** (ADOPTING.md §4): routing saves only when tasks carry facts; how to get the agents that write tasks to state them.
- **OpenClaw bindings** (ROUTING.md §9.3, ADOPTING.md §9): routing for long-running agents that take their model from config: `openclaw-config`, `openclaw-observe`, the `openclaw-sessions` usage reader.
- **Usage probes** (`hive-route probe`): subscription window shares where no output file reports them.
- **Routes may list the `kinds` they serve** (RT2), e.g. a review-only route.
- Requests carry `runtime` (logged and reported, never used to decide) and `estimated_facts` (from the scorer).
- Health checks can send a provider's auth header, so a rejected API key marks its pool down.
- Python 3.10 is enough.

**Adopters:** nothing required; every addition is optional. To use checkpoints, see ADOPTING.md §4, "Checkpoints". If your tasks carry no facts, read "Facts" there: without them live mode routes everything to your strongest tier.

## rev 5 — 2026-09-30

- Routes through the gateway (`via_gateway`) for self-hosted chat-completions servers and API keys; health-checked pools fall back when their endpoint is down.
- Metered pools end to end: spend from the gateway, budgets per period and per attempt, a worker key for agents.
- `canary accept`: an operator can qualify a route below the suite's bar, with a reason.
- A pool's readings from several sources are merged; the launcher re-declares an actor when its model changes (hive-record SPEC §6.2).
- `docs/ADOPTING.md` and `examples/launch.sh`.

**Adopters:** none required for subscription-only hives. Gateway routes need the gateway (ADOPTING.md §5).

## rev 4 — 2026-09-29

Every build step implemented: usage tracking, drift checks through attempt manifests, canaries with a starter suite, the scorer (shadow), shadow tables, `whatif` replay, the outcomes `report`, a generated gateway config, and recording on the hive record (amendment A1).

## rev 3 — 2026-09-29

`decide()` specified exactly (inputs, four outcomes, the time it reads); qualification bound to the route pin; failure class `interrupted`; a fixed usage-state shape; aligned with the frozen hive record (SPEC v1.0).
