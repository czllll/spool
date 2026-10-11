"""Totals per category and overall for an expenses.csv (date,merchant,category,amount)."""

import csv
import sys
from collections import defaultdict

totals: dict[str, float] = defaultdict(float)
with open(sys.argv[1], newline="") as fh:
    for row in csv.DictReader(fh):
        totals[row["category"].strip().lower()] += float(row["amount"])
for category in sorted(totals):
    print(f"{category}: {totals[category]:.2f}")
print(f"total: {sum(totals.values()):.2f}")
