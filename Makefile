.PHONY: verify verify-paper self-test compare-results bootstrap lock-check tables figures

verify:
	./run.py verify

verify-paper:
	./run.py verify-paper

self-test:
	./run.py self-test

compare-results:
	./run.py compare-results

bootstrap:
	./scripts/bootstrap.sh

lock-check:
	uv lock --check

tables:
	uv run --locked --extra analysis ./run.py reproduce --tables-only

figures:
	uv run --locked --extra analysis ./run.py reproduce
