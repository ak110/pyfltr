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
# --config明示指定はprekのworkspace再帰探索（サブディレクトリの.pre-commit-config.yamlも
# 実行対象へ含める仕様）を無効化するため（prek 0.4.11で確認）。
setup:
	$(MAKE) clean-stale-dist-info
	uv sync --all-groups --all-extras
	uvx prek --config=.pre-commit-config.yaml install
	git config --local commit.template .gitmessage

# 依存パッケージをアップグレードし全テスト実行
update:
	$(MAKE) clean-stale-dist-info
	env --unset UV_FROZEN uv sync --upgrade --all-groups --all-extras
	uvx prek --config=.pre-commit-config.yaml autoupdate
	$(MAKE) update-actions
	$(MAKE) update-docker-base
	$(MAKE) test

# GitHub Actionsのアクションをハッシュピンで最新化（mise未導入時はスキップ）
update-actions:
	@command -v mise >/dev/null 2>&1 || { echo "mise未検出、スキップ"; exit 0; }; \
	GITHUB_TOKEN=$$(gh auth token) mise exec -- pinact run --update --min-age=1

# Dockerベースイメージのdigestピンを最新化（docker未導入時はスキップ）
# 更新対象は ``AS base`` のdigestピン付きFROM行1件に限定し、当該行のタグを取得して当該行だけを置換する
# 対象行が1件でない場合、digestを取得できない場合、置換後の行が期待と一致しない場合は書き換えずに失敗させる
update-docker-base:
	@command -v docker >/dev/null 2>&1 || { echo "docker未検出、スキップ"; exit 0; }; \
	before=$$(grep -cE '^FROM python:[^@ ]+@sha256:[0-9a-f]+ AS base$$' docker/Dockerfile || true); \
	if [ "$$before" != "1" ]; then echo "digestピン付きのbase行が1件でない（$$before件）: docker/Dockerfile" >&2; exit 1; fi; \
	image="$$(sed -nE 's|^FROM (python:[^@ ]+)@sha256:[0-9a-f]+ AS base$$|\1|p' docker/Dockerfile)"; \
	digest="$$(docker buildx imagetools inspect "$$image" | awk '/^Digest:/{print $$2; exit}')"; \
	case "$$digest" in sha256:*) ;; *) echo "digest取得に失敗: $$image" >&2; exit 1;; esac; \
	tmp="$$(mktemp)"; \
	sed -E "s|^FROM python:[^@ ]+@sha256:[0-9a-f]+ AS base$$|FROM $$image@$$digest AS base|" docker/Dockerfile > "$$tmp"; \
	after=$$(grep -cxF "FROM $$image@$$digest AS base" "$$tmp" || true); \
	if [ "$$after" != "1" ]; then rm -f "$$tmp"; echo "digestの置換に失敗（$$after件）: $$image" >&2; exit 1; fi; \
	cat "$$tmp" > docker/Dockerfile; rm -f "$$tmp"; \
	grep -nxF "FROM $$image@$$digest AS base" docker/Dockerfile

# フォーマット + 軽量lint（開発時の手動実行用。自動修正あり）
# pyfltr fast は fix ステージを内蔵するため、以前の `pyfltr fix` に相当する自動修正も実行される
format:
	$(MAKE) clean-stale-dist-info
	uv run pyfltr fast

# 全チェック実行（これを通過すればコミット可能）
test:
	$(MAKE) clean-stale-dist-info
	uv run pyfltr run

docs:
	uv run mkdocs serve

.PHONY: help clean-stale-dist-info setup update update-actions update-docker-base format test docs
