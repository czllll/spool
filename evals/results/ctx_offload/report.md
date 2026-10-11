# Eval: ctx_offload

- **ctx_offload**: model `local`, 3 trials/task, config `evals/configs/ctx_offload.toml`

## Summary

| config | trials | success (95% CI) | pass^3 | steps | input tok | cache reuse | prefill ms/call | wall s (median) | prefix breaks | tool err |
|---|---|---|---|---|---|---|---|---|---|---|
| ctx_offload | 12 | 33% (14%–61%) | 25% | 15.8 | 77,868 | 89% | 6,148 | 866 | 0.0 | 2% |

## ctx_offload: per task

| task | passed | median steps | median wall s | failures |
|---|---|---|---|---|
| long/fix-calculator | 0/3 | 16 | 2400 | did_not_finish:timeout×2, looping×1 |
| long/log-incident | 3/3 | 8 | 355 | – |
| long/todo-audit | 1/3 | 19 | 846 | wrong_result_claimed_done×1, ran_out_of_steps×1 |
| long/wiki-research | 0/3 | 20 | 886 | ran_out_of_steps×3 |

Failure categories: did_not_finish:timeout 2, looping 1, ran_out_of_steps 4, wrong_result_claimed_done 1
