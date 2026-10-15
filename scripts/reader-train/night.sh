#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Train, calibrate and evaluate the fine-tuned reader on the host GPU (any time; the GPU is free to take).
# Stops kev.service and kev-gate.service (they hold the GPU) and always restarts them.
#
#   scripts/reader-train/night.sh <kev-data-dir> <run-name> <eval-dir>
#
# Scheduled with: systemd-run --user --unit reader-ft-night \
#   bash <hive-route>/scripts/reader-train/night.sh ...
set -u
DATA=$1; NAME=$2; EVAL=$3
KEV=$HOME/opt/kev; HR=$HOME/repos/hive-route; UV=$HOME/.local/bin/uv
RUN=$KEV/runs/$NAME
mkdir -p "$EVAL"; LOG=$EVAL/night.log
exec >>"$LOG" 2>&1
say() { echo "[$(date -Is)] $*"; }
SERVE_PID=
restore() {
  [ -n "$SERVE_PID" ] && kill "$SERVE_PID" 2>/dev/null && wait "$SERVE_PID" 2>/dev/null
  say "restarting kev.service and kev-gate.service"
  systemctl --user start kev.service; systemctl --user start kev-gate.service
  for i in $(seq 1 90); do curl -s -m 2 -o /dev/null 127.0.0.1:8009 && break; sleep 2; done
  say "kev :8009 $(curl -s -m 5 -o /dev/null -w '%{http_code}' 127.0.0.1:8009); kev.service $(systemctl --user is-active kev.service); kev-gate.service $(systemctl --user is-active kev-gate.service)"
}
trap restore EXIT
trap "exit 1" TERM INT HUP

say "stopping kev-gate.service and kev.service"
systemctl --user stop kev-gate.service kev.service
sleep 5; nvidia-smi --query-gpu=memory.used --format=csv

cd "$KEV" || exit 1
train() {
  say "training $NAME $*"
  "$UV" run --frozen python -m kev.train --data "$DATA/train.jsonl" --base Qwen/Qwen3.5-4B-Base \
    --init_from jaredpalmer/kev-4b@v1.0 --epochs 2 --lr 2e-5 --batch 1 --accum 8 --dtype bf16 \
    --checkpointing 1 --device cuda --max_state 2048 --out "$RUN" "$@"
}
if ! train; then
  say "training failed; retrying with bf16 backbone weights and a row budget"
  rm -rf "$RUN"
  train --weights_dtype bf16 --row_budget 2560 || { say "training failed twice"; exit 1; }
fi

say "temperature fit and in-distribution evaluation"
for only in fine_tuned baseline; do  # one model per process: the GPU doesn't hold both
  "$UV" run --frozen python "$HR/scripts/reader-train/calibrate_run.py" --run "$RUN" --base jaredpalmer/kev-4b@v1.0 \
    --data "$DATA" --out "$EVAL/heldout" --only $only || say "calibrate_run --only $only failed"
done

say "serving $NAME on :8019"
KEV_PREFIX_CACHE=0 KEV_CUDA_GRAPHS=0 PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True \
  "$UV" run --frozen --extra serve python -m kev.serve --run "$RUN" --host 127.0.0.1 --port 8019 \
  > "$EVAL/serve.log" 2>&1 &
SERVE_PID=$!
for i in $(seq 1 120); do curl -s -m 2 -o /dev/null 127.0.0.1:8019 && break; sleep 3; done
cd "$HR" || exit 1
"$UV" run hive-route scorer-eval "$EVAL/table.yaml" fixtures/scorer/portable.jsonl --scorer kev-ft \
  --out "$EVAL/ft-portable.jsonl" > "$EVAL/ft-portable.summary.json"
"$UV" run hive-route scorer-eval "$EVAL/table.yaml" fixtures/scorer/suite.jsonl --scorer kev-ft \
  --workspace "$HOME/repos/hive-workspace.git" --commit 90ca75fd99ed30475c45999bb99c9ef0e1af43e7 \
  --out "$EVAL/ft-suite.jsonl" > "$EVAL/ft-suite.summary.json"
say "done"
