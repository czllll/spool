# Eval: crash

- **crash**: model `local`, 4 trials/task, config `evals/configs/baseline.toml`

## Summary

| config | trials | success (95% CI) | pass^3 | steps | input tok | cache reuse | prefill ms/call | wall s (median) | prefix breaks | tool err |
|---|---|---|---|---|---|---|---|---|---|---|
| crash | 4 | 100% (51%–100%) | 100% | 3.2 | 5,697 | 73% | 7,342 | 224 | 0.0 | 0% |

## Crash recovery

| config | crashed trials | finished after resume | passed | extra messages sent |
|---|---|---|---|---|
| crash | 4 | 100% | 100% | 0 |

## crash: per task

| task | passed | median steps | median wall s | failures |
|---|---|---|---|---|
| core/send-weekly-report | 4/4 | 3 | 224 | – |
