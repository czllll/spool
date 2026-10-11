#!/usr/bin/env bash
# Reproduce every number in the report. Needs the local model server:
#   scripts/serve_local_model.sh &   (Qwen3.5-4B on :8089, 3 slots)
# Finished trials are skipped on re-run, so this can be interrupted and resumed.
set -euo pipefail
cd "$(dirname "$0")/.."
SPOOL=".venv/bin/spool"
R=evals/results
mkdir -p "$R"

run() {  # name, then eval args
  local name="$1"; shift
  mkdir -p "$R/$name"
  echo "=== $name ($(date '+%H:%M:%S'))"
  "$SPOOL" eval --name "$name" --out "$R/$name" "$@" 2>&1 | tee -a "$R/$name/log.txt" | grep -E "^\[|PASS|fail" || true
}

# Main result: the default harness on every task.
run baseline        --config evals/configs/baseline.toml        --suite evals/tasks/core --suite evals/tasks/long --trials 3 --concurrency 3
# E1: prefix stability. Same harness, wall clock at the top of the system prompt.
run unstable_prefix --config evals/configs/unstable_prefix.toml --suite evals/tasks/core --trials 3 --concurrency 3
# E4: crash recovery. Each trial is SIGKILLed at one of these points, then resumed in a new process.
run crash           --config evals/configs/baseline.toml --suite evals/tasks/core \
                    --tasks send-weekly-report --trials 4 --concurrency 2 \
                    --crash "after_tool_executed:send_message:1,after_model_response::2,after_tool_started::2,after_tool_executed::1"
run crash_more      --config evals/configs/baseline.toml --suite evals/tasks/core \
                    --tasks fix-off-by-one --trials 4 --concurrency 2 \
                    --crash "after_model_response::2,after_tool_started::2,after_tool_executed::1,after_tool_executed::3"
# Failure 0002 follow-ups: (v2) ask for fewer plan updates in the prompt; (v3) don't charge plan-only turns.
run baseline_v2     --config evals/configs/baseline_v2.toml     --suite evals/tasks/core --trials 3 --concurrency 3
run baseline_v3     --config evals/configs/baseline_v3.toml     --suite evals/tasks/core --trials 3 --concurrency 3
# E2: context strategies on the long tasks (40-minute budget per task).
run ctx_offload     --config evals/configs/ctx_offload.toml   --suite evals/tasks/long --trials 3 --concurrency 3
run ctx_full        --config evals/configs/ctx_full.toml      --suite evals/tasks/long --trials 3 --concurrency 3
run ctx_fold        --config evals/configs/ctx_fold.toml      --suite evals/tasks/long --trials 3 --concurrency 3
run ctx_summarize   --config evals/configs/ctx_summarize.toml --suite evals/tasks/long --trials 3 --concurrency 3
# Failure 0004 follow-up: offload only under context pressure.
run ctx_adaptive    --config evals/configs/ctx_adaptive.toml  --suite evals/tasks/long --trials 3 --concurrency 3

# E5: actions on the demo services through MCP connectors, approvals answered by a simulated user;
# then the same with the agent SIGKILLed right after an order is placed / an email is sent.
run actions         --config evals/configs/actions.toml --suite evals/tasks/actions --trials 3 --concurrency 3
run actions_crash   --config evals/configs/actions.toml --suite evals/tasks/actions --tasks two-items --trials 3 \
                    --concurrency 3 --crash "after_tool_executed:shop__place_order:1"
run actions_crash_mail --config evals/configs/actions.toml --suite evals/tasks/actions --tasks reply-to-landlord \
                    --trials 3 --concurrency 3 --crash "after_tool_executed:mail__send_message:1"

"$SPOOL" report "$R/baseline" "$R/unstable_prefix" "$R/crash" "$R/crash_more" "$R/baseline_v2" "$R/baseline_v3" \
  "$R/ctx_offload" "$R/ctx_full" "$R/ctx_fold" "$R/ctx_summarize" "$R/ctx_adaptive" \
  "$R/actions" "$R/actions_crash" "$R/actions_crash_mail" \
  -o "$R/REPORT.md" --title "Spool evals (Qwen3.5-4B, local)"
.venv/bin/python scripts/summarize_results.py > "$R/HEADLINE.md"
echo "done $(date '+%H:%M:%S')"
