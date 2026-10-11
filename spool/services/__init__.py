"""Demo services, each an MCP server over stdio backed by one SQLite file.

    python -m spool.services.mail     --db mail.sqlite     [--seed mail.json]   [show]
    python -m spool.services.calendar --db calendar.sqlite [--seed cal.json]    [show]
    python -m spool.services.shop     --db shop.sqlite     [--seed shop.json]   [show]

They stand in for a user's real accounts in the demo and in the evals: state
persists across restarts (so a crashed agent resumes against the same
mailbox), every write takes an idempotency key (a repeated key returns the
first result instead of acting twice), and `show` prints the state for
verifiers and people.
"""
