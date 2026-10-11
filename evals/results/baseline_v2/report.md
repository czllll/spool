# Eval: baseline_v2

- **baseline_v2**: model `local`, 3 trials/task, config `evals/configs/baseline_v2.toml`

## Summary

| config | trials | success (95% CI) | pass^3 | steps | input tok | cache reuse | prefill ms/call | wall s (median) | prefix breaks | tool err |
|---|---|---|---|---|---|---|---|---|---|---|
| baseline_v2 | 36 | 86% (71%–94%) | 75% | 6.0 | 13,540 | 79% | 6,431 | 287 | 0.0 | 0% |

## baseline_v2: per task

| task | passed | median steps | median wall s | failures |
|---|---|---|---|---|
| core/config-edit | 3/3 | 5 | 137 | – |
| core/csv-region-totals | 3/3 | 4 | 256 | – |
| core/dedupe-emails | 2/3 | 6 | 387 | wrong_result_claimed_done×1 |
| core/fix-off-by-one | 3/3 | 7 | 314 | – |
| core/greeting | 3/3 | 2 | 55 | – |
| core/implement-slugify | 3/3 | 8 | 407 | – |
| core/json-to-csv | 3/3 | 4 | 281 | – |
| core/log-error-counts | 3/3 | 7 | 354 | – |
| core/rename-extensions | 3/3 | 4 | 156 | – |
| core/rename-function | 0/3 | 15 | 573 | ran_out_of_steps×3 |
| core/send-weekly-report | 2/3 | 3 | 441 | wrong_result_claimed_done×1 |
| core/word-frequency | 3/3 | 6 | 297 | – |

Failure categories: ran_out_of_steps 3, wrong_result_claimed_done 2
