# Eval: baseline

- **baseline**: model `local`, 3 trials/task, config `evals/configs/baseline.toml`

## Summary

| config | trials | success (95% CI) | pass^3 | steps | input tok | cache reuse | prefill ms/call | wall s (median) | prefix breaks | tool err |
|---|---|---|---|---|---|---|---|---|---|---|
| baseline | 48 | 77% (63%–87%) | 75% | 7.5 | 22,530 | 84% | 5,778 | 304 | 0.0 | 1% |

## baseline: per task

| task | passed | median steps | median wall s | failures |
|---|---|---|---|---|
| core/config-edit | 3/3 | 5 | 148 | – |
| core/csv-region-totals | 3/3 | 4 | 248 | – |
| core/dedupe-emails | 3/3 | 6 | 236 | – |
| core/fix-off-by-one | 3/3 | 6 | 311 | – |
| core/greeting | 3/3 | 2 | 42 | – |
| core/implement-slugify | 3/3 | 6 | 328 | – |
| core/json-to-csv | 3/3 | 6 | 275 | – |
| core/log-error-counts | 3/3 | 6 | 259 | – |
| core/rename-extensions | 3/3 | 4 | 180 | – |
| core/rename-function | 0/3 | 15 | 624 | ran_out_of_steps×3 |
| core/send-weekly-report | 3/3 | 3 | 350 | – |
| core/word-frequency | 3/3 | 6 | 327 | – |
| long/fix-calculator | 0/3 | 8 | 900 | did_not_finish:timeout×3 |
| long/log-incident | 3/3 | 7 | 321 | – |
| long/todo-audit | 1/3 | 17 | 853 | did_not_finish:timeout×1, wrong_result_claimed_done×1 |
| long/wiki-research | 0/3 | 20 | 826 | did_not_finish:timeout×1, ran_out_of_steps×2 |

Failure categories: did_not_finish:timeout 5, ran_out_of_steps 5, wrong_result_claimed_done 1
