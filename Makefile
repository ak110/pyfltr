# サプライチェーン攻撃対策としてlockfileを常に尊重する。依存を更新する場合のみ
# `env --unset UV_FROZEN` で一時的に無効化する（`UV_FROZEN=` の空文字代入はuvがエラー扱い）。
export UV_FROZEN := 1

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

# 開発環境のセットアップ
setup:
	$(MAKE) clean-stale-dist-info
	uv sync --all-groups --all-extras
	uv run pre-commit install

# 依存パッケージをアップグレードし全テスト実行
update:
	$(MAKE) clean-stale-dist-info
	env --unset UV_FROZEN uv sync --upgrade --all-groups --all-extras
	uv run pre-commit autoupdate
	$(MAKE) update-actions
	$(MAKE) test

# GitHub Actionsのアクションをハッシュピンで最新化（mise未導入時はスキップ）
update-actions:
	@command -v mise >/dev/null 2>&1 || { echo "mise未検出、スキップ"; exit 0; }; \
	GITHUB_TOKEN=$$(gh auth token) mise exec -- pinact run --update --min-age 1

# フォーマット + 軽量lint（開発時の手動実行用。自動修正あり）
# pyfltr fast は fix ステージを内蔵するため、以前の `pyfltr fix` に相当する自動修正も走る
# 利用者向け推奨と同じ with 方式で起動するが、ローカル変更を反映するため editable 指定にする
format:
	$(MAKE) clean-stale-dist-info
	uv run --with-editable=. pyfltr fast

# 全チェック実行（これを通過すればコミット可能）
test:
	$(MAKE) clean-stale-dist-info
	uv run --with-editable=. pyfltr run

docs:
	uv run mkdocs serve

.PHONY: help clean-stale-dist-info setup update update-actions format test docs
