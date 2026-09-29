# hive-route

A slim model-routing service for One Hive (release R8): named routes, one canary per route, detectable model switches. Works for hives on API keys, subscriptions, local models, or a mix.

Status: in build. See [`ROUTING.md`](ROUTING.md) (rev 3). The one-pager [`ROUTING-onepager.pdf`](ROUTING-onepager.pdf) is still rev 2.

Build step 1 is done: the route table, a deterministic `decide()`, rule fixtures and a replayable JSONL decision log.

```sh
uv sync --extra test
uv run hive-route check examples/1-hive.yaml                  # validate; print table and route pins
uv run hive-route decide examples/1-hive.yaml request.json \
    --state state.json --mode fixed --log route.jsonl          # exit 0 = route, 3 = wait/no_route/reconcile
uv run hive-route replay route.jsonl                          # recompute every logged decision
uv run pytest
```

- `schemas/`: the table, request and state formats.
- `fixtures/`: one case per rule, each with its expected decision.
- `examples/1-hive.yaml`: 1-hive's pools and routes as of 2026-09-29, with today's launcher as the `fixed` baseline.

Related: [hive-record](https://github.com/1-hive/hive-record), [1-hive](https://github.com/1-hive/1-hive).
