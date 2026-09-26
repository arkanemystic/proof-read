.PHONY: test test-fast
test:
	uv run pytest -q
test-fast:
	uv run pytest -q -m "not slow and not docker and not biject and not network"
