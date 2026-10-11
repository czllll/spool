.PHONY: install test serve evals report tasks

install:
	uv venv --python 3.12 && uv pip install -e '.[dev]'

test:
	.venv/bin/python -m pytest -q

serve:            ## local model server (Qwen3.5-4B via llama.cpp) on :8089
	scripts/serve_local_model.sh

tasks:            ## regenerate eval task data (deterministic)
	.venv/bin/python evals/build_tasks.py

evals:            ## every number in evals/results/REPORT.md (needs `make serve`)
	scripts/run_evals.sh

report:
	.venv/bin/spool report evals/results/* -o evals/results/REPORT.md
