# Spool evals (Qwen3.5-4B, local)

- **baseline**: model `local`, 3 trials/task, config `evals/configs/baseline.toml`
- **unstable_prefix**: model `local`, 3 trials/task, config `evals/configs/unstable_prefix.toml`
- **crash**: model `local`, 4 trials/task, config `evals/configs/baseline.toml`
- **crash_more**: model `local`, 4 trials/task, config `evals/configs/baseline.toml`
- **baseline_v2**: model `local`, 3 trials/task, config `evals/configs/baseline_v2.toml`
- **ctx_offload**: model `local`, 3 trials/task, config `evals/configs/ctx_offload.toml`
- **ctx_full**: model `local`, 3 trials/task, config `evals/configs/ctx_full.toml`
- **ctx_fold**: model `local`, 3 trials/task, config `evals/configs/ctx_fold.toml`
- **ctx_summarize**: model `local`, 3 trials/task, config `evals/configs/ctx_summarize.toml`

## Summary

| config | trials | success (95% CI) | pass^3 | steps | input tok | cache reuse | prefill ms/call | wall s (median) | prefix breaks | tool err |
|---|---|---|---|---|---|---|---|---|---|---|
| baseline | 48 | 77% (63%–87%) | 75% | 7.5 | 22,530 | 84% | 5,778 | 304 | 0.0 | 1% |
| unstable_prefix | 36 | 86% (71%–94%) | 75% | 5.5 | 12,248 | 8% | 20,599 | 507 | 4.5 | 0% |
| crash | 4 | 100% (51%–100%) | 100% | 3.2 | 5,697 | 73% | 7,342 | 224 | 0.0 | 0% |
| crash_more | 4 | 100% (51%–100%) | 100% | 6.5 | 13,180 | 71% | 7,140 | 204 | 0.0 | 5% |
| baseline_v2 | 36 | 86% (71%–94%) | 75% | 6.0 | 13,540 | 79% | 6,431 | 287 | 0.0 | 0% |
| ctx_offload | 12 | 33% (14%–61%) | 25% | 15.8 | 77,868 | 89% | 6,148 | 866 | 0.0 | 2% |
| ctx_full | 12 | 67% (39%–86%) | 50% | 14.0 | 81,485 | 88% | 8,680 | 598 | 0.0 | 1% |
| ctx_fold | 12 | 25% (9%–53%) | 25% | 15.2 | 65,134 | 80% | 8,589 | 869 | 1.0 | 1% |
| ctx_summarize | 12 | 58% (32%–81%) | 50% | 12.1 | 56,988 | 82% | 12,436 | 774 | 0.3 | 1% |

## Crash recovery

| config | crashed trials | finished after resume | passed | extra messages sent |
|---|---|---|---|---|
| crash | 4 | 100% | 100% | 0 |
| crash_more | 4 | 100% | 100% | 0 |

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

## crash: per task

| task | passed | median steps | median wall s | failures |
|---|---|---|---|---|
| core/send-weekly-report | 4/4 | 3 | 224 | – |

## crash_more: per task

| task | passed | median steps | median wall s | failures |
|---|---|---|---|---|
| core/fix-off-by-one | 4/4 | 6 | 204 | – |

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

## ctx_offload: per task

| task | passed | median steps | median wall s | failures |
|---|---|---|---|---|
| long/fix-calculator | 0/3 | 16 | 2400 | did_not_finish:timeout×2, looping×1 |
| long/log-incident | 3/3 | 8 | 355 | – |
| long/todo-audit | 1/3 | 19 | 846 | wrong_result_claimed_done×1, ran_out_of_steps×1 |
| long/wiki-research | 0/3 | 20 | 886 | ran_out_of_steps×3 |

Failure categories: did_not_finish:timeout 2, looping 1, ran_out_of_steps 4, wrong_result_claimed_done 1

## ctx_full: per task

| task | passed | median steps | median wall s | failures |
|---|---|---|---|---|
| long/fix-calculator | 1/3 | 21 | 2297 | wrong_result_claimed_done×1, did_not_finish:timeout×1 |
| long/log-incident | 3/3 | 7 | 288 | – |
| long/todo-audit | 1/3 | 25 | 1043 | ran_out_of_steps×2 |
| long/wiki-research | 3/3 | 7 | 390 | – |

Failure categories: did_not_finish:timeout 1, ran_out_of_steps 2, wrong_result_claimed_done 1

## ctx_fold: per task

| task | passed | median steps | median wall s | failures |
|---|---|---|---|---|
| long/fix-calculator | 0/3 | 13 | 2400 | wrong_result_claimed_done×1, did_not_finish:timeout×2 |
| long/log-incident | 3/3 | 7 | 285 | – |
| long/todo-audit | 0/3 | 25 | 889 | ran_out_of_steps×2, wrong_result_claimed_done×1 |
| long/wiki-research | 0/3 | 20 | 813 | ran_out_of_steps×3 |

Failure categories: did_not_finish:timeout 2, ran_out_of_steps 5, wrong_result_claimed_done 2

## ctx_summarize: per task

| task | passed | median steps | median wall s | failures |
|---|---|---|---|---|
| long/fix-calculator | 0/3 | 14 | 2400 | did_not_finish:timeout×3 |
| long/log-incident | 3/3 | 6 | 359 | – |
| long/todo-audit | 1/3 | 25 | 1102 | ran_out_of_steps×2 |
| long/wiki-research | 3/3 | 6 | 469 | – |

Failure categories: did_not_finish:timeout 3, ran_out_of_steps 2
