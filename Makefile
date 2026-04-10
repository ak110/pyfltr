# サプライチェーン攻撃対策としてlockfileを常に尊重する。依存を更新する場合のみ
# `env --unset UV_FROZEN` で一時的に無効化する（`UV_FROZEN=` の空文字代入はuvがエラー扱い）。
export UV_FROZEN := 1

UV_RUN := uv run --all-extras --all-groups

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
	env --unset UV_FROZEN uv sync --upgrade --all-extras --all-groups
	$(UV_RUN) pre-commit autoupdate
	$(MAKE) update-actions
	$(MAKE) test

# GitHub Actions のアクションをハッシュピンで最新化 (mise 未導入時はスキップ)
update-actions:
	@command -v mise >/dev/null 2>&1 || { echo "mise が見つかりません。スキップします。"; exit 0; }; \
	GITHUB_TOKEN=$$(gh auth token) mise exec -- pinact run --update --min-age 1

# フォーマット + 軽量 lint (開発時の手動実行用。自動修正あり)
format:
	$(MAKE) clean-stale-dist-info
	SKIP=pyfltr $(UV_RUN) pre-commit run --all-files
	-$(UV_RUN) pyfltr fast

# 全チェック実行 (このタスクが成功したらコミット可能)
test:
	$(MAKE) clean-stale-dist-info
	SKIP=pyfltr $(UV_RUN) pre-commit run --all-files
	$(UV_RUN) pyfltr run

docs:
	$(UV_RUN) mkdocs serve

.PHONY: help clean-stale-dist-info update update-actions format test docs
