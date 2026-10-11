# ADR 0006: Outcome-graded evals with pass^k, run on a local model

Status: accepted

## Context

Claims like "offloading helps" or "the cache works" need numbers, and the numbers need to be
reproducible by someone else with one command. No hosted model API key is available while building.

## Decision

- Task = `setup/` + prompt + `verify.py` (`spool/eval/task.py`). The verifier checks the final state
  of the workspace and the side-effect ledger, never the steps taken (Anthropic, "Demystifying
  evals for AI agents", 2026-01-09). It lives outside the workspace.
- Every verifier is itself tested: the untouched workspace must fail and a reference solution must
  pass (`tests/test_eval_tasks.py`).
- Each task runs 3+ trials. Reports give success with a Wilson 95% interval, pass@k and pass^k
  (all k trials succeed; from tau-bench), tokens, cache reuse, prefill time per call, prefix
  breaks, render mismatches and a failure taxonomy.
- Results append to `results.jsonl` per trial, so runs resume after interruption.
- Models: Qwen3.5-4B (Q4_K_M) through llama.cpp on an M1 laptop. The same command runs any
  OpenAI-compatible or Anthropic model via `--model`.

## Consequences

- Absolute success rates reflect a 4B model, not the harness's ceiling; comparisons are only made
  between configurations on the same model.
- With 3 trials per task, intervals are wide; reports say so instead of rounding them away.
- `scripts/run_evals.sh` regenerates every number in `evals/results/REPORT.md`.
