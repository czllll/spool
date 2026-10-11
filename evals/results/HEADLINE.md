### Baseline (default harness)

| configuration | trials | success (95% CI) | pass^3 | prompt tokens from cache | prefill ms per call | median wall s | median peak prompt |
|---|---|---|---|---|---|---|---|
| core tasks (12), 15-min budget | 36 | 92% (78%–97%) | 92% | 77% | 6,013 | 272 | 2,507 |
| long tasks (4), 15-min budget | 12 | 33% (14%–61%) | 25% | 89% | 5,076 | 839 | 4,833 |
| long tasks (4), 40-min budget | 12 | 33% (14%–61%) | 25% | 89% | 6,148 | 866 | 8,054 |

### E1: prefix stability (core tasks)

| configuration | trials | success (95% CI) | pass^3 | prompt tokens from cache | prefill ms per call | median wall s | median peak prompt |
|---|---|---|---|---|---|---|---|
| stable prefix (baseline) | 36 | 92% (78%–97%) | 92% | 77% | 6,013 | 272 | 2,507 |
| clock at top of system prompt | 36 | 86% (71%–94%) | 75% | 8% | 20,599 | 507 | 2,542 |

### Failure 0002 follow-ups: too many plan updates (core tasks)

| configuration | trials | success (95% CI) | pass^3 | prompt tokens from cache | prefill ms per call | median wall s | median peak prompt |
|---|---|---|---|---|---|---|---|
| baseline prompt | 36 | 92% (78%–97%) | 92% | 77% | 6,013 | 272 | 2,507 |
| baseline_v2 prompt | 36 | 86% (71%–94%) | 75% | 79% | 6,431 | 287 | 2,559 |
| baseline_v3: plan-only turns not charged | 36 | 89% (75%–96%) | 75% | 79% | 6,949 | 316 | 2,495 |

### E2: context strategy (long tasks, 40-min budget)

| configuration | trials | success (95% CI) | pass^3 | prompt tokens from cache | prefill ms per call | median wall s | median peak prompt |
|---|---|---|---|---|---|---|---|
| full history | 12 | 67% (39%–86%) | 50% | 88% | 8,680 | 598 | 8,402 |
| fixed-threshold offload (old default) | 12 | 33% (14%–61%) | 25% | 89% | 6,148 | 866 | 8,054 |
| reversible fold | 12 | 25% (9%–53%) | 25% | 80% | 8,589 | 869 | 6,344 |
| summarize | 12 | 58% (32%–81%) | 50% | 82% | 12,436 | 774 | 5,575 |
| budget-aware offload (new default) | 12 | 67% (39%–86%) | 50% | 89% | 8,789 | 722 | 5,749 |

### E4: crash recovery (SIGKILL, then resume in a new process)

| task | trials | crashed | finished after resume | passed | messages sent per trial |
|---|---|---|---|---|---|
| core/send-weekly-report | 4 | 4 | 4/4 | 4/4 | 1, 1, 1, 1 |
| core/fix-off-by-one | 4 | 4 | 4/4 | 4/4 | 0, 0, 0, 0 |

### Replay check

Every recorded request re-rendered from the log: 1,869 requests, 0 mismatches.
