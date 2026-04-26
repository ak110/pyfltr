# GitHub Actions 等の CI 用に pyfltr と周辺ツール (uv / pnpm / mise / hadolint 等) を
# 同梱する公式イメージ。``ghcr.io/ak110/pyfltr`` として公開する。
#
# サプライチェーン攻撃対策として、ベースイメージは digest pin で固定し、
# uv / pnpm / mise の各キャッシュディレクトリは ``/cache/{uv,pnpm,mise}`` に集約する
# (GitHub Actions の ``actions/cache`` などから既知パスで参照しやすくするため)。
#
# 非 root ユーザー ``pyfltr`` で実行する。CLI ツール用途のため ``HEALTHCHECK`` は設定しない。

# hadolint global ignore: 未指定タグ警告は digest pin を使うため不要。
# hadolint global ignore=DL3007

# Stage 1: Python ランタイム + uv ベース。
# Python 3.14 (slim) の digest を固定する。
# Renovate / Dependabot 等が更新しやすいよう ``image:tag@sha256`` の素直な形にする。
FROM python:3.14-slim-bookworm@sha256:b594fc4b73c8c63a45bb1ef13e62a82e8e63e6ce0ea6ea4ebe2e4f3ecf5fe5be AS base

ARG PYFLTR_VERSION=""

# パイプを使う RUN で `set -o pipefail` 相当の挙動を得るため、SHELL を明示する
# (hadolint DL4006 対応)。
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

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
    PATH=/usr/local/pnpm:/root/.local/bin:/usr/local/bin:/usr/bin:/bin

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
# ``minimum-release-age`` (1440 分 = 24 時間) はサプライチェーン攻撃対策の標準設定で、
# 公開直後の新バージョン導入を抑止する。
RUN --mount=type=cache,target=/root/.npm \
    npm install -g --prefix /usr/local pnpm@latest \
    && pnpm config set store-dir "${PNPM_STORE_DIR}" --global \
    && pnpm config set minimum-release-age 1440 --global

# mise バイナリを導入する。
RUN curl -fsSL https://mise.run | sh \
    && mv /root/.local/bin/mise /usr/local/bin/mise

# hadolint バイナリを導入する (Dockerfile lint 用)。
RUN curl -fsSL -o /usr/local/bin/hadolint \
        https://github.com/hadolint/hadolint/releases/latest/download/hadolint-Linux-x86_64 \
    && chmod +x /usr/local/bin/hadolint

# pyfltr 本体を導入する。
# ``PYFLTR_VERSION`` が空の場合はリリース最新版、指定時は当該バージョンを固定する。
# pip のダウンロードキャッシュをマウントで再利用する。
# hadolint ignore=DL3013
RUN --mount=type=cache,target=/root/.cache/pip \
    if [ -n "${PYFLTR_VERSION}" ]; then \
        pip install "pyfltr[python]==${PYFLTR_VERSION}" ; \
    else \
        pip install "pyfltr[python]" ; \
    fi

# キャッシュディレクトリを作成し、非 root ユーザーが書き込めるよう所有権を移譲する。
RUN useradd --create-home --shell /bin/bash pyfltr \
    && mkdir -p /cache/uv /cache/pnpm /cache/mise/data /cache/mise/cache \
    && chown -R pyfltr:pyfltr /cache /usr/local/pnpm

USER pyfltr
WORKDIR /workspace

ENTRYPOINT ["pyfltr"]
CMD ["--help"]
