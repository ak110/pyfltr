
help:
	@cat Makefile

update:
	poetry update
	$(MAKE) test

test:
	poetry install
	poetry run pyfltr --exit-zero-even-if-formatted
