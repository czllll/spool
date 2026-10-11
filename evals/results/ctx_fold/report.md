# Eval: ctx_fold

- **ctx_fold**: model `local`, 3 trials/task, config `evals/configs/ctx_fold.toml`

## Summary

| config | trials | success (95% CI) | pass^3 | steps | input tok | cache reuse | prefill ms/call | wall s (median) | prefix breaks | tool err |
|---|---|---|---|---|---|---|---|---|---|---|
| ctx_fold | 12 | 25% (9%–53%) | 25% | 15.2 | 65,134 | 80% | 8,589 | 869 | 1.0 | 1% |

## ctx_fold: per task

| task | passed | median steps | median wall s | failures |
|---|---|---|---|---|
| long/fix-calculator | 0/3 | 13 | 2400 | wrong_result_claimed_done×1, did_not_finish:timeout×2 |
| long/log-incident | 3/3 | 7 | 285 | – |
| long/todo-audit | 0/3 | 25 | 889 | ran_out_of_steps×2, wrong_result_claimed_done×1 |
| long/wiki-research | 0/3 | 20 | 813 | ran_out_of_steps×3 |

Failure categories: did_not_finish:timeout 2, ran_out_of_steps 5, wrong_result_claimed_done 2
