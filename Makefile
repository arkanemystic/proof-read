.PHONY: test test-fast
test:
	uv run pytest -q
test-fast:
	uv run pytest -q -m "not slow and not docker and not biject and not network"

.PHONY: demo
# Section 10d live demo (real run, about 4 to 5 minutes, at most 3 USD). Rehearse with: make demo ARGS=--dry-run
demo:
	uv run python scripts/demo.py $(ARGS)
