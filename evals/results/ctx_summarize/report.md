# Eval: ctx_summarize

- **ctx_summarize**: model `local`, 3 trials/task, config `evals/configs/ctx_summarize.toml`

## Summary

| config | trials | success (95% CI) | pass^3 | steps | input tok | cache reuse | prefill ms/call | wall s (median) | prefix breaks | tool err |
|---|---|---|---|---|---|---|---|---|---|---|
| ctx_summarize | 12 | 58% (32%–81%) | 50% | 12.1 | 56,988 | 82% | 12,436 | 774 | 0.3 | 1% |

## ctx_summarize: per task

| task | passed | median steps | median wall s | failures |
|---|---|---|---|---|
| long/fix-calculator | 0/3 | 14 | 2400 | did_not_finish:timeout×3 |
| long/log-incident | 3/3 | 6 | 359 | – |
| long/todo-audit | 1/3 | 25 | 1102 | ran_out_of_steps×2 |
| long/wiki-research | 3/3 | 6 | 469 | – |

Failure categories: did_not_finish:timeout 3, ran_out_of_steps 2
