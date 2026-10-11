---
name: expense-report
description: Turn receipts into an expense report in the company format. Use it when the task mentions receipts, expenses or reimbursement.
---

# Expense report

1. Collect the receipts the task points to (files, or emails with amounts).
2. Write `expenses.csv` with the header `date,merchant,category,amount` (ISO dates, amounts with two
   decimals, categories: travel, meals, office, other).
3. Run `python3 .skills/expense-report/scripts/total.py expenses.csv` and copy its output (totals per
   category and overall) into `expense-report.md` under the heading `# Expense report`.
