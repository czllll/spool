# Eval: unstable_prefix

- **unstable_prefix**: model `local`, 3 trials/task, config `evals/configs/unstable_prefix.toml`

## Summary

| config | trials | success (95% CI) | pass^3 | steps | input tok | cache reuse | prefill ms/call | wall s (median) | prefix breaks | tool err |
|---|---|---|---|---|---|---|---|---|---|---|
| unstable_prefix | 36 | 86% (71%–94%) | 75% | 5.5 | 12,248 | 8% | 20,599 | 507 | 4.5 | 0% |

## unstable_prefix: per task

| task | passed | median steps | median wall s | failures |
|---|---|---|---|---|
| core/config-edit | 3/3 | 5 | 216 | – |
| core/csv-region-totals | 2/3 | 5 | 533 | did_not_finish:timeout×1 |
| core/dedupe-emails | 3/3 | 5 | 508 | – |
| core/fix-off-by-one | 2/3 | 6 | 413 | did_not_finish:timeout×1 |
| core/greeting | 3/3 | 2 | 111 | – |
| core/implement-slugify | 3/3 | 6 | 640 | – |
| core/json-to-csv | 3/3 | 6 | 640 | – |
| core/log-error-counts | 3/3 | 6 | 574 | – |
| core/rename-extensions | 3/3 | 4 | 310 | – |
| core/rename-function | 0/3 | 11 | 900 | did_not_finish:timeout×3 |
| core/send-weekly-report | 3/3 | 3 | 723 | – |
| core/word-frequency | 3/3 | 6 | 457 | – |

Failure categories: did_not_finish:timeout 5
