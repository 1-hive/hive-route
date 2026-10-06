# Changelog

What changed in each revision of hive-route, and what a hive running it needs to do. Revisions follow `ROUTING.md`'s `rev`. **Adopters** says whether an existing deployment must change anything.

## rev 8 — 2026-10-06

- **The route service** (ROUTING.md §9.2, ADOPTING.md §10): `hive-route serve` answers `POST /route` at the start of each episode of an agent with its own loop (Iter), and takes `POST /episodes/end` with the episode's class. Facts at stake are fixed per agent in an agents file (`schemas/agents-v1.schema.json`, `examples/agents.yaml`); the agent states only `specification` and `scope` per episode. The service keeps each agent's episode history and logs every decision, which replays. Standard library only, no new dependency.
- `decide` and the service share one path for scoring, deciding and logging.

**Adopters:** nothing required. To route Iter agents per episode, add the hook in ADOPTING.md §10 and stop binding those agents.

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
