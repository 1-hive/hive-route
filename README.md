# hive-route

A slim model-routing service for One Hive (release R8): named routes, one canary per route, detectable model switches. Works for hives on API keys, subscriptions, local models, or a mix.

Status: every build step is built; 1-hive runs it in `fixed` mode. See [`ROUTING.md`](ROUTING.md) (rev 4) and the one-pager [`ROUTING-onepager.pdf`](ROUTING-onepager.pdf).

```sh
uv sync --extra test
uv run hive-route check TABLE                           # validate; print table and route pins
uv run hive-route state TABLE SOURCES                   # pool usage and qualifications, now
uv run hive-route decide TABLE REQUEST --sources SOURCES --log LOG [--task-text FILE] [--shadow-table T2]
                                                        # exit 0 = route, 3 = wait/no_route/reconcile
uv run hive-route mode fixed|live TABLE --log LOG       # record a mode change
uv run hive-route manifest DECISION --cwd DIR --output FILE   # attempt manifest, for drift checks
uv run hive-route observe LOG 'GLOB/*.attempt.json' --sources SOURCES   # exit 4 = drift
uv run hive-route canary run TABLE ROUTE --suite canaries/starter --sources SOURCES --log LOG
uv run hive-route replay LOG                            # recompute every logged decision
uv run hive-route whatif LOG TABLE2                     # the log under another table
uv run hive-route report LOG --events EVENTS --agentsview ROOT   # outcomes by kind, facts, tier, route
uv run hive-route record LOG                            # summaries to the hive record (A1)
uv run hive-route gateway-config TABLE                  # LiteLLM config from the table
uv run pytest
```

- `schemas/`: table, request, state, sources, attempt manifest and canary suite formats.
- `fixtures/`: one case per rule, each with its expected decision.
- `canaries/starter/`: the shared starter canary suite.
- `examples/1-hive.yaml`: 1-hive's table (mirrors `1-hive/deploy/route-table.yaml`).
- `docs/make-onepager.py`: builds the one-pager.

Related: [hive-record](https://github.com/1-hive/hive-record), [1-hive](https://github.com/1-hive/1-hive).
