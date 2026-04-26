# syntax=docker/dockerfile:1
# GitHub Actions 等の CI 用に pyfltr と pyfltr が対応する formatter / linter / tester、
# および周辺ツール (uv / pnpm / mise / hadolint 等) を同梱する公式イメージ。
# ``ghcr.io/ak110/pyfltr`` として公開する。
#
# サプライチェーン攻撃対策として、ベースイメージは digest pin で固定し、
# uv 側は ``~/.config/uv/uv.toml`` の ``exclude-newer = "1 day"``、
# pnpm 側は ``minimum-release-age=1440`` 分で公開直後の新バージョン導入を抑止する。
# pyfltr 自身はリリース直後に本イメージをビルドするため、
# ``exclude-newer-package = { pyfltr = false }`` で除外対象から外す。
# uv / pnpm / mise の各キャッシュディレクトリは ``/cache/{uv,pnpm,mise}`` に集約する
# (GitHub Actions の ``actions/cache`` などから既知パスで参照しやすくするため)。
#
# 非 root ユーザー ``pyfltr`` で実行する。CLI ツール用途のため ``HEALTHCHECK`` は設定しない。
#
# マルチステージ構成: 頻繁に更新される pyfltr 導入レイヤーを ``final`` ステージ末尾に
# 切り出し、``base`` ステージで構築する基盤一式 (APT / uv / pnpm / mise / 各種ツール群 /
# 非 root ユーザー) のキャッシュを最大限再利用する。
#
# 同梱ツール一覧 (新ツール追加時は本リストと該当 RUN 層を更新する):
#   - Python (pyfltr[python] 経由): ruff / mypy / pylint / pyright / ty / pytest / uv-sort
#   - Python (pyfltr 本体依存): typos / pre-commit
#   - Python 単独 (uv tool install): yamllint
#   - mise 経由のネイティブバイナリ: shfmt / actionlint / editorconfig-checker / glab /
#     taplo / hadolint / pinact / gitleaks
#   - APT 直結の補助ツール: shellcheck (direct runner フォールバック用)
#   - pnpm グローバル経由の JS / TS 系: prettier / markdownlint-cli / textlint / eslint /
#     @biomejs/biome / oxlint / typescript / vitest
#
# Rust (cargo-*) と .NET (dotnet-*) のツールチェインはイメージサイズへの影響が大きいため
# 同梱しない。利用者側で `mise use rust@latest` 等を経由して導入する想定。

# hadolint global ignore: 未指定タグ警告は digest pin を使うため不要。
# hadolint global ignore=DL3007

# Stage 1: 基盤構築。
# Python 3.14 (slim) の digest を固定する。
# Renovate / Dependabot 等が更新しやすいよう ``image:tag@sha256`` の素直な形にする。
FROM python:3.14-slim-bookworm@sha256:2e256d0381371566ed96980584957ed31297f437569b79b0e5f7e17f2720e53a AS base

# パイプを使う RUN で `set -o pipefail` 相当の挙動を得るため、SHELL を明示する
# (hadolint DL4006 対応)。
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# uv 側のサプライチェーン攻撃対策は ``~/.config/uv/uv.toml`` で
# ``exclude-newer = "1 day"`` を指定する (USER pyfltr 切り替え後に配置)。
# 実行時はユーザーが ``UV_EXCLUDE_NEWER`` 環境変数で上書きできる。
# PATH 先頭の ``/home/pyfltr/.local/bin`` は ``uv tool install`` が配置する CLI を解決する。
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_CACHE_DIR=/cache/uv \
    UV_LINK_MODE=copy \
    UV_FROZEN=1 \
    PNPM_HOME=/usr/local/pnpm \
    PNPM_STORE_DIR=/cache/pnpm \
    MISE_DATA_DIR=/cache/mise/data \
    MISE_CACHE_DIR=/cache/mise/cache \
    PATH=/home/pyfltr/.local/bin:/cache/mise/data/shims:/usr/local/pnpm:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# 必要な APT パッケージをまとめてインストールする。
# ``--no-install-recommends`` で推奨パッケージを除外し、イメージサイズを抑える。
# BuildKit のキャッシュマウントで apt キャッシュをビルド間で再利用する
# (cf. <https://docs.docker.com/build/cache/optimize/#use-cache-mounts>)。
# Debian 公式イメージは ``/etc/apt/apt.conf.d/docker-clean`` で apt キャッシュを
# 自動削除するため、キャッシュマウント有効時のみそれを除去する。
# hadolint ignore=DL3008
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    rm -f /etc/apt/apt.conf.d/docker-clean \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        gnupg \
        jq \
        nodejs \
        npm \
        shellcheck

# uv を astral 公式インストーラ経由で導入する。
RUN curl -LsSf https://astral.sh/uv/install.sh | sh \
    && mv /root/.local/bin/uv /usr/local/bin/uv \
    && mv /root/.local/bin/uvx /usr/local/bin/uvx

# pnpm スタンドアロン版を導入する。npm のグローバルキャッシュもマウントで再利用する。
RUN --mount=type=cache,target=/root/.npm \
    npm install -g --prefix /usr/local pnpm@latest

# mise バイナリを導入する。
RUN curl -fsSL https://mise.run | sh \
    && mv /root/.local/bin/mise /usr/local/bin/mise

# 非 root ユーザーを作成し、キャッシュ・pnpm ホームの所有権を移譲する。
# UID 1001 は GitHub Actions の runner ユーザーと一致させるため固定する。
# ``container:`` 配下で動かす際、runner が用意する ``/__w/_temp`` 等のホストマウント領域は
# UID 1001 所有で作成されるため、コンテナー側ユーザーが UID 1001 でないと
# ``actions/checkout`` 等が ``EACCES`` で失敗する。
RUN useradd --create-home --shell /bin/bash --uid 1001 pyfltr \
    && mkdir -p /cache/uv /cache/pnpm /cache/mise/data /cache/mise/cache /usr/local/pnpm \
    && chown -R pyfltr:pyfltr /cache /usr/local/pnpm

USER pyfltr
WORKDIR /workspace

# uv のサプライチェーン保護設定を ``~/.config/uv/uv.toml`` に配置する。
# ``exclude-newer = "1 day"`` で公開から 1 日経過していないパッケージを除外し、
# pyfltr 自身はリリース直後の本イメージビルドで毎回除外対象になるため
# ``exclude-newer-package`` で例外指定する (自リポジトリの成果物のため対象外でよい)。
RUN mkdir -p /home/pyfltr/.config/uv \
    && printf '%s\n%s\n' \
        'exclude-newer = "1 day"' \
        'exclude-newer-package = { pyfltr = false }' \
        > /home/pyfltr/.config/uv/uv.toml

# pyfltr が呼び出す対応ツール群を pyfltr ユーザー権限で事前導入する。
# 単一 RUN にまとめてレイヤー数とイメージサイズを抑える (新ツール追加時はここに追記する)。
#
# - pnpm 設定: ``minimum-release-age=1440`` 分 = 24 時間で公開直後の新バージョン導入を抑止。
#   ``store-dir`` は ``PNPM_STORE_DIR`` env でも効くが、CI から pnpm を直接呼ぶ場合の
#   一貫性のため設定にも明示する。
# - mise 経由 (bin-runner=mise 既定): pyfltr のネイティブバイナリ群。インストール先は
#   ``MISE_DATA_DIR=/cache/mise/data`` に集約され、レイヤーに持続する。
# - pnpm グローバル経由 (js-runner=pnpx 既定): JS / TS 系ツール。pnpx 経由でも同一ストアを
#   参照するため初回起動コストが下がる。
# - uv tool install: pyfltr[python] に含まれない Python 単独ツール。
RUN pnpm config set store-dir "${PNPM_STORE_DIR}" --global \
    && pnpm config set minimum-release-age 1440 --global \
    && mise use --global \
        shfmt@latest \
        actionlint@latest \
        editorconfig-checker@latest \
        glab@latest \
        taplo@latest \
        hadolint@latest \
        pinact@latest \
        gitleaks@latest \
    && pnpm add -g \
        prettier \
        markdownlint-cli \
        textlint \
        eslint \
        "@biomejs/biome" \
        oxlint \
        typescript \
        vitest \
    && uv tool install yamllint

# Stage 2: pyfltr 本体導入。
# このステージは ``PYFLTR_VERSION`` の更新ごとに再実行されるため、``base`` のキャッシュを
# 汚さないように末尾の 1 レイヤーに局所化する。
FROM base AS final

ARG PYFLTR_VERSION=""

# pyfltr 本体を ``uv tool install`` で導入する。
# ``PYFLTR_VERSION`` が空の場合はリリース最新版、指定時は当該バージョンを固定する。
# 非 root ユーザー ``pyfltr`` 配下に配置されるため、所有権の調整は不要。
RUN if [ -n "${PYFLTR_VERSION}" ]; then \
        uv tool install "pyfltr[python]==${PYFLTR_VERSION}" ; \
    else \
        uv tool install "pyfltr[python]" ; \
    fi

ENTRYPOINT ["pyfltr"]
CMD ["--help"]
