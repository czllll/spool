# Eval: crash_more

- **crash_more**: model `local`, 4 trials/task, config `evals/configs/baseline.toml`

## Summary

| config | trials | success (95% CI) | pass^3 | steps | input tok | cache reuse | prefill ms/call | wall s (median) | prefix breaks | tool err |
|---|---|---|---|---|---|---|---|---|---|---|
| crash_more | 4 | 100% (51%–100%) | 100% | 6.5 | 13,180 | 71% | 7,140 | 204 | 0.0 | 5% |

## Crash recovery

| config | crashed trials | finished after resume | passed | extra messages sent |
|---|---|---|---|---|
| crash_more | 4 | 100% | 100% | 0 |

## crash_more: per task

| task | passed | median steps | median wall s | failures |
|---|---|---|---|---|
| core/fix-off-by-one | 4/4 | 6 | 204 | – |
