
help:
	@cat Makefile

clean-stale-dist-info:
	@for d in .venv/lib/python*/site-packages/pyfltr-*.dist-info; do \
		[ -d "$$d" ] || continue; \
		if [ ! -f "$$d/RECORD" ]; then \
			echo "Removing stale dist-info: $$d"; \
			\rm -rf "$$d"; \
		fi; \
	done

update:
	$(MAKE) clean-stale-dist-info
	uv sync --upgrade --all-extras --all-groups
	uv run pre-commit autoupdate
	$(MAKE) test

format:
	$(MAKE) clean-stale-dist-info
	SKIP=pyfltr uv run pre-commit run --all-files
	-uv run pyfltr --exit-zero-even-if-formatted --commands=fast

test:
	$(MAKE) clean-stale-dist-info
	SKIP=pyfltr uv run pre-commit run --all-files
	uv run pyfltr --exit-zero-even-if-formatted

docs:
	uv run mkdocs serve

.PHONY: help clean-stale-dist-info update format test docs
