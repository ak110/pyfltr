# サプライチェーン攻撃対策としてlockfileを常に尊重する。依存を更新する場合のみ
# `env --unset UV_FROZEN` で一時的に無効化する（`UV_FROZEN=` の空文字代入はuvがエラー扱い）。
export UV_FROZEN := 1

help:
	@cat Makefile

clean-stale-dist-info:
	@for d in .venv/lib/python*/site-packages/pyfltr-*.dist-info; do \
		[ -d "$$d" ] || continue; \
		if [ ! -f "$$d/RECORD" ]; then \
			echo "古いdist-infoを自動削除します（対応不要）: $$d"; \
			\rm -rf "$$d"; \
		fi; \
	done

# 仮想環境を、ロックファイルが選ぶ全依存グループ・extrasと`.python-version`の基準Python版へそろえる。
# `uv sync`は余分なパッケージを除き、`--python`は`UV_PYTHON`などで別の版が指定された環境でも
# 基準の版で仮想環境を再作成する。`uv run`の自動同期は余分なパッケージを残すため、この同期に代えない（uv 0.13.1で確認）。
sync:
	$(MAKE) clean-stale-dist-info
	uv sync --all-groups --all-extras --python "$$(cat .python-version)"

# 開発環境のセットアップ
# --config明示指定はprekのworkspace再帰探索（サブディレクトリの.pre-commit-config.yamlも
# 実行対象へ含める仕様）を無効化するため（prek 0.4.11で確認）。
setup: sync
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
	@command -v mise >/dev/null 2>&1 || { echo "mise未検出のためスキップします。アクションを更新する場合はmiseを導入して make update-actions を再実行してください。"; exit 0; }; \
	GITHUB_TOKEN=$$(gh auth token) mise exec -- pinact run --update --min-age=1

# Dockerベースイメージのdigestピンを最新化（docker未導入時はスキップ）
# 更新対象は ``AS base`` のdigestピン付きFROM行1件に限定し、対象の行のタグを取得して対象の行だけを置換する
# 対象行が1件でない場合、digestを取得できない場合、置換後の行が期待と一致しない場合は書き換えずに失敗させる
update-docker-base:
	@command -v docker >/dev/null 2>&1 || { echo "docker未検出のためスキップします。digestを更新する場合はdockerを導入して make update-docker-base を再実行してください。"; exit 0; }; \
	before=$$(grep -cE '^FROM python:[^@ ]+@sha256:[0-9a-f]+ AS base$$' docker/Dockerfile || true); \
	if [ "$$before" != "1" ]; then echo "digestピン付きのbase行が1件でない（$$before件）: docker/Dockerfile。digestピン付きの FROM python:... AS base 行を1件に直して再実行してください。" >&2; exit 1; fi; \
	image="$$(sed -nE 's|^FROM (python:[^@ ]+)@sha256:[0-9a-f]+ AS base$$|\1|p' docker/Dockerfile)"; \
	digest="$$(docker buildx imagetools inspect "$$image" | awk '/^Digest:/{print $$2; exit}')"; \
	case "$$digest" in sha256:*) ;; *) echo "digest取得に失敗: $$image。docker buildx imagetools inspect で対象イメージの取得可否を確認して再実行してください。" >&2; exit 1;; esac; \
	tmp="$$(mktemp)"; \
	sed -E "s|^FROM python:[^@ ]+@sha256:[0-9a-f]+ AS base$$|FROM $$image@$$digest AS base|" docker/Dockerfile > "$$tmp"; \
	after=$$(grep -cxF "FROM $$image@$$digest AS base" "$$tmp" || true); \
	if [ "$$after" != "1" ]; then rm -f "$$tmp"; echo "digestの置換に失敗（$$after件）: $$image。Dockerfileは変更していません。docker/Dockerfileのdigestピン付きの FROM python:... AS base 行を確認して再実行してください。" >&2; exit 1; fi; \
	cat "$$tmp" > docker/Dockerfile; rm -f "$$tmp"; \
	grep -nxF "FROM $$image@$$digest AS base" docker/Dockerfile

# フォーマット + 軽量lint（開発時の手動実行用。自動修正あり）
# pyfltr fast は fix ステージを内蔵するため、以前の `pyfltr fix` に相当する自動修正も実行される
format: sync
	uv run pyfltr fast

# 全チェック実行（これを通過すればコミット可能）
test: sync
	uv run pyfltr run

docs:
	uv run mkdocs serve

.PHONY: help clean-stale-dist-info sync setup update update-actions update-docker-base format test docs
