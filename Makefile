
help:
	@cat Makefile

update:
	uv sync --all-extras --dev
	$(MAKE) test

format:
	uv run pyfltr --exit-zero-even-if-formatted --commands=fast

test:
	uv run pyfltr --exit-zero-even-if-formatted

.PHONY: help update format test
