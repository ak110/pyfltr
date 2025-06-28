
help:
	@cat Makefile

update:
	uv sync --upgrade --all-extras --all-groups
	uv run pre-commit autoupdate
	$(MAKE) test

format:
	-uv run pyfltr --exit-zero-even-if-formatted --commands=fast --no-ui

test:
	uv run pre-commit run --all-files
	uv run pyfltr --exit-zero-even-if-formatted

.PHONY: help update format test
