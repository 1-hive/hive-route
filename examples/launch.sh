#!/usr/bin/env bash
# A minimal launcher that routes each attempt through hive-route (docs/ADOPTING.md §4).
# Start from this; 1-hive's tools/launch-task.sh is a fuller example (record, re-declaration).
#
#   launch.sh <kind> <task-id> <attempt-n> <kickoff-file> <workdir>
#
#   kind      work | review | plan | consult | triage | digest
#   env:      ROUTE_FACTS  '{"specification":"explicit","verification":"independent",...}'
#             ROUTE_HINT   '{"tier":"strong","reason":"..."}'   (can only raise the tier)
#             AUTHOR       '{"family":"claude","tier":"strong"}' (required for a review)
#             ROUTE_HISTORY '[{"attempt":...,"route_id":...,"tier":...,"class":...}]'
#
# Exit 0: started (prints the pid). Exit 3: the router said wait, no_route or reconcile.
set -euo pipefail
KIND=$1 TASK=$2 N=$3 KICKOFF=$4 WORK=$5
ID='^[a-z0-9][a-z0-9._-]{0,63}$'
[[ "$TASK" =~ $ID && "$N" =~ ^[0-9]+$ ]] || { echo "bad task id or attempt number" >&2; exit 2; }

# ---- deployment settings: edit these -----------------------------------------------------
TABLE=${TABLE:-$HOME/hive/route-table.yaml}          # your route table (docs/ADOPTING.md §2)
SOURCES=${SOURCES:-$HOME/hive/route-sources.yaml}    # usage sources, harnesses, gateway (§3)
STATE=${STATE:-$HOME/hive/route}                     # the router's log and attempt manifests
GATEWAY=${GATEWAY:-http://127.0.0.1:4000}            # only for via_gateway routes (§5)
GATEWAY_KEY_FILE=${GATEWAY_KEY_FILE:-$HOME/.config/hive/gateway.env}   # HIVE_WORKER_KEY=...
hr() { hive-route "$@"; }                            # or: uv run --project <hive-route> hive-route
# ------------------------------------------------------------------------------------------

mkdir -p "$STATE"
LOG=$STATE/route-log.jsonl
[ -f "$LOG" ] || hr mode fixed "$TABLE" --log "$LOG" >/dev/null   # start in fixed mode (§6)
hr observe "$LOG" "$STATE/*.attempt.json" --sources "$SOURCES" >&2 || true   # drift checks

ATTEMPT=$TASK.$KIND.$N
REQ=$(jq -n --arg t "$TASK" --arg a "$ATTEMPT" --arg k "$KIND" \
  --argjson facts "${ROUTE_FACTS:-{\}}" --argjson hint "${ROUTE_HINT:-null}" \
  --argjson author "${AUTHOR:-null}" --argjson history "${ROUTE_HISTORY:-[]}" \
  '{task: $t, attempt: $a, facts: ({kind: $k} + $facts)}
   + (if $hint then {hint: $hint} else {} end) + (if $author then {author: $author} else {} end)
   + (if ($history | length) > 0 then {history: $history} else {} end)')

RC=0; DEC=$(printf '%s' "$REQ" | hr decide "$TABLE" - --sources "$SOURCES" --log "$LOG" \
  --task-text "$KICKOFF") || RC=$?
if [ "$RC" -ne 0 ]; then
  echo "router: $(jq -r '.decision + (if .wait_until then " until " + .wait_until else "" end)' <<<"$DEC")" >&2
  jq -r '.reasons[] | "  " + .rule + ": " + .note' <<<"$DEC" >&2
  exit 3
fi
HARNESS=$(jq -r .harness <<<"$DEC") MODEL=$(jq -r .model <<<"$DEC") EFFORT=$(jq -r '.effort // empty' <<<"$DEC")
echo "route: $(jq -r '.route_id + " (" + .model + "), tier " + .tier' <<<"$DEC")"

# Routes through the gateway ask for the route's alias, with a key that may only call models.
ENVS=(); GWARGS=()
if [ "$(jq -r '.via_gateway // false' <<<"$DEC")" = true ]; then
  MODEL=$(jq -r .route_id <<<"$DEC")
  WKEY=$(sed -n 's/^HIVE_WORKER_KEY=//p' "$GATEWAY_KEY_FILE")
  case "$HARNESS" in
    claude-code) ENVS=(ANTHROPIC_BASE_URL="$GATEWAY" ANTHROPIC_AUTH_TOKEN="$WKEY" ANTHROPIC_API_KEY=
                       ANTHROPIC_CUSTOM_HEADERS="x-litellm-tags: task:$TASK,attempt:$ATTEMPT") ;;
    codex) ENVS=(HIVE_WORKER_KEY="$WKEY")
           GWARGS=(-c 'model_providers.hivegw.name="hive gateway"' -c "model_providers.hivegw.base_url=\"$GATEWAY/v1\""
                   -c 'model_providers.hivegw.env_key="HIVE_WORKER_KEY"' -c 'model_providers.hivegw.wire_api="responses"'
                   -c 'model_provider="hivegw"') ;;
  esac
  # A metered route: cap this attempt at the gateway too (the master key stays with the launcher).
  AB=$(printf '%s' "$DEC" | hr attempt-budget "$TABLE" -)
  if [ -n "$AB" ]; then
    MKEY=$(sed -n 's/^HIVE_GATEWAY_KEY=//p' "$GATEWAY_KEY_FILE")
    curl -sf -X POST "$GATEWAY/tag/new" -H "Authorization: Bearer $MKEY" -H 'Content-Type: application/json' \
      -d "$AB" >/dev/null || { echo "couldn't create the attempt's budget" >&2; exit 3; }
  fi
fi

cd "$WORK"
case "$HARNESS" in
  claude-code)
    OUT=$WORK/$KIND-$N.jsonl
    nohup env "${ENVS[@]}" claude -p --model "$MODEL" ${EFFORT:+--effort "$EFFORT"} \
      --permission-mode auto --output-format stream-json --verbose "$(cat "$KICKOFF")" > "$OUT" 2> "${OUT%.*}.err" & ;;
  codex)
    OUT=$WORK/$KIND-$N.log
    nohup env "${ENVS[@]}" codex exec --skip-git-repo-check --cd "$WORK" "${GWARGS[@]}" -m "$MODEL" \
      ${EFFORT:+-c model_reasoning_effort="$EFFORT"} - < "$KICKOFF" > "$OUT" 2>&1 & ;;
  *) echo "this launcher can't start harness $HARNESS" >&2; exit 3 ;;
esac
PID=$!
# The manifest lets `hive-route observe` check which model actually ran (drift, §3).
printf '%s' "$DEC" | hr manifest - --cwd "$WORK" --output "$OUT" > "$STATE/$ATTEMPT.attempt.json"
echo "pid $PID, output $OUT"
