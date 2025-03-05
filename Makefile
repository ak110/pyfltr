
help:
	@cat Makefile

update:
	uv sync --all-extras --dev
	$(MAKE) test

test:
	uv run pyfltr --exit-zero-even-if-formatted
