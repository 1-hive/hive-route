#!/usr/bin/env bash
# Keep long-running OpenClaw agents on the router's routes (ROUTING.md §9.3,
# docs/ADOPTING.md §9). Run it every few minutes from a timer (systemd or cron).
#
# Each run: refreshes subscription usage with a probe when due (hive-route probe), renders
# every binding (openclaw-config), and, when a config's patch changed, validates it with
# OpenClaw (a dry run) and applies it. OpenClaw hot-applies model settings: no restart.
# Then it checks the agents' turns for drift (openclaw-observe).
#
# If your hive changes agent config only through its own controller or review process,
# replace apply() with a call to it: the patch files are plain JSON merge patches.
set -euo pipefail

# ---- deployment settings: edit these -----------------------------------------------------
TABLE=${TABLE:-$HOME/hive/route-table.yaml}          # your route table (docs/ADOPTING.md §2)
BINDINGS=${BINDINGS:-$HOME/hive/route-bindings.yaml} # your bindings (examples/openclaw-bindings.yaml)
SOURCES=${SOURCES:-$HOME/hive/route-sources.yaml}    # usage readers, probes, harnesses (§3)
STATE=${STATE:-$HOME/hive/route}                     # the router's log and the patches
openclaw() { command openclaw "$@"; }                # e.g. node /path/to/openclaw/openclaw.mjs "$@",
                                                     # or docker exec <container> openclaw "$@"
hr() { hive-route "$@"; }                            # or: uv run --project <hive-route> hive-route
# ------------------------------------------------------------------------------------------

mkdir -p "$STATE/patches" "$STATE/applied"
LOG=$STATE/route-log.jsonl

apply() {  # <openclaw config file> <patch file>
  OPENCLAW_CONFIG_PATH=$1 openclaw config patch --file "$2" --dry-run >/dev/null \
    && OPENCLAW_CONFIG_PATH=$1 openclaw config patch --file "$2"
}

hr probe "$SOURCES" || echo "a usage probe failed; usage may be stale" >&2

# exit 8: a patch names a model the config doesn't allow or a provider it lacks
RC=0
OUT=$(hr openclaw-config "$TABLE" "$BINDINGS" --sources "$SOURCES" --log "$LOG" --check \
        --write-dir "$STATE/patches") || RC=$?
[ "$RC" -eq 0 ] || { echo "openclaw-config: exit $RC; nothing applied" >&2; exit "$RC"; }

# Apply each config's patch once: only when it differs from the one applied last.
jq -r '.patches | keys[]' <<<"$OUT" | while read -r CONFIG; do
  NAME=$(basename "$CONFIG" .json)
  P=$STATE/patches/$NAME.patch.json
  if ! cmp -s "$P" "$STATE/applied/$NAME.patch.json"; then
    if apply "$CONFIG" "$P"; then
      cp "$P" "$STATE/applied/$NAME.patch.json"
      echo "applied: $CONFIG"
    else
      echo "not applied (OpenClaw refused it): $CONFIG" >&2
    fi
  fi
done

hr openclaw-observe "$LOG" --sources "$SOURCES" || echo "drift detected; see $LOG" >&2
