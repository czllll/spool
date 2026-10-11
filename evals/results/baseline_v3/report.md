# Eval: baseline_v3

- **baseline_v3**: model `local`, 3 trials/task, config `evals/configs/baseline_v3.toml`

## Summary

| config | trials | success (95% CI) | pass^3 | steps | input tok | cache reuse | prefill ms/call | wall s (median) | prefix breaks | tool err |
|---|---|---|---|---|---|---|---|---|---|---|
| baseline_v3 | 36 | 89% (75%–96%) | 75% | 6.0 | 12,964 | 79% | 6,949 | 316 | 0.0 | 0% |

## baseline_v3: per task

| task | passed | median steps | median wall s | failures |
|---|---|---|---|---|
| core/config-edit | 3/3 | 5 | 167 | – |
| core/csv-region-totals | 2/3 | 4 | 290 | did_not_finish:timeout×1 |
| core/dedupe-emails | 3/3 | 5 | 321 | – |
| core/fix-off-by-one | 3/3 | 6 | 371 | – |
| core/greeting | 3/3 | 2 | 69 | – |
| core/implement-slugify | 2/3 | 6 | 442 | did_not_finish:timeout×1 |
| core/json-to-csv | 3/3 | 4 | 312 | – |
| core/log-error-counts | 1/3 | 5 | 274 | wrong_result_claimed_done×2 |
| core/rename-extensions | 3/3 | 4 | 157 | – |
| core/rename-function | 3/3 | 16 | 732 | – |
| core/send-weekly-report | 3/3 | 6 | 436 | – |
| core/word-frequency | 3/3 | 6 | 351 | – |

Failure categories: did_not_finish:timeout 2, wrong_result_claimed_done 2
