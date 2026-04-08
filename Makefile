
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
	$(MAKE) update-actions
	$(MAKE) test

# GitHub Actions のアクションをハッシュピンで最新化 (mise 未導入時はスキップ)
update-actions:
	@command -v mise >/dev/null 2>&1 || { echo "mise が見つかりません。スキップします。"; exit 0; }; \
	GITHUB_TOKEN=$$(gh auth token) mise exec -- pinact run --update --min-age 1

# フォーマット + 軽量 lint (開発時の手動実行用。自動修正あり)
format:
	$(MAKE) clean-stale-dist-info
	SKIP=pyfltr uv run pre-commit run --all-files
	-uv run pyfltr --exit-zero-even-if-formatted --commands=fast

# 全チェック実行 (このタスクが成功したらコミット可能)
test:
	$(MAKE) clean-stale-dist-info
	SKIP=pyfltr uv run pre-commit run --all-files
	uv run pyfltr --exit-zero-even-if-formatted

docs:
	uv run mkdocs serve

.PHONY: help clean-stale-dist-info update update-actions format test docs
