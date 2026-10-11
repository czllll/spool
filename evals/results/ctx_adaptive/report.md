# Eval: ctx_adaptive

- **ctx_adaptive**: model `local`, 3 trials/task, config `evals/configs/ctx_adaptive.toml`

## Summary

| config | trials | success (95% CI) | pass^3 | steps | input tok | cache reuse | prefill ms/call | wall s (median) | prefix breaks | tool err |
|---|---|---|---|---|---|---|---|---|---|---|
| ctx_adaptive | 12 | 67% (39%–86%) | 50% | 12.9 | 62,365 | 89% | 8,789 | 722 | 0.0 | 0% |

## ctx_adaptive: per task

| task | passed | median steps | median wall s | failures |
|---|---|---|---|---|
| long/fix-calculator | 0/3 | 17 | 2400 | did_not_finish:timeout×2, looping×1 |
| long/log-incident | 3/3 | 9 | 467 | – |
| long/todo-audit | 2/3 | 18 | 953 | ran_out_of_steps×1 |
| long/wiki-research | 3/3 | 7 | 481 | – |

Failure categories: did_not_finish:timeout 2, looping 1, ran_out_of_steps 1
