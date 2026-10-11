# Eval: ctx_full

- **ctx_full**: model `local`, 3 trials/task, config `evals/configs/ctx_full.toml`

## Summary

| config | trials | success (95% CI) | pass^3 | steps | input tok | cache reuse | prefill ms/call | wall s (median) | prefix breaks | tool err |
|---|---|---|---|---|---|---|---|---|---|---|
| ctx_full | 12 | 67% (39%–86%) | 50% | 14.0 | 81,485 | 88% | 8,680 | 598 | 0.0 | 1% |

## ctx_full: per task

| task | passed | median steps | median wall s | failures |
|---|---|---|---|---|
| long/fix-calculator | 1/3 | 21 | 2297 | wrong_result_claimed_done×1, did_not_finish:timeout×1 |
| long/log-incident | 3/3 | 7 | 288 | – |
| long/todo-audit | 1/3 | 25 | 1043 | ran_out_of_steps×2 |
| long/wiki-research | 3/3 | 7 | 390 | – |

Failure categories: did_not_finish:timeout 1, ran_out_of_steps 2, wrong_result_claimed_done 1
