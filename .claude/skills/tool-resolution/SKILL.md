---
name: tool-resolution
description: >
  pyfltrのツール解決方針。
  mise backend登録・python-runner/js-runner/bin-runner委譲・uv/uvx/pnpx/pnpm/npm/yarn等の直接指定値・
  fallback検出と~展開対象キー・対応ツールの依存方針を集約する。
  pyfltr/command/runner.py・pyfltr/command/mise.py・pyfltr/config/config.py・
  pyfltr/command/builtin.py・pyfltr/command/dispatcher.py・
  docs/guide/recommended*.md・docs/guide/configuration*.md・
  tests/config_test.py・tests/command_info_test.py を編集する際に使用する。
---

# pyfltrのツール解決方針

pyfltrが対応するformatter/linter/testerの依存指定および実行時のツール解決順位の方針を集約する。

## 対応ツールの依存方針

依存指定は次の基準で振り分ける。

- 本体依存（`dependencies`）: 本家公式かつ自己完結なPyPIパッケージ
  - 汎用的に有用なもの（例: `typos`、`pre-commit`）
  - Python系ツール一式（例: `ruff`、`mypy`、`pylint`、`pyright`、`ty`、`pytest`、`uv-sort`）。
    `uvx pyfltr`単発で揃うようにし、`{command}-runner = "python-runner"`既定でグローバル
    `python-runner = "uv"`へ委譲し、cwdの`uv.lock`検出時は利用者プロジェクトの登録版へ切り替える。
    `pyright[nodejs]` extrasはNode.jsランタイム取得を伴うが、Python系ツール一式を`uvx pyfltr`単発で
    揃える利便性を優先して同梱する
- 依存指定なし: 本家から独立した個人または別組織のメンテに依存するもの、
  インストール時に外部バイナリを取得するもの、Node.js等のランタイムを伴うもの

サードパーティの非公式PyPIラッパー（例: `shfmt-py`・`actionlint-py`・`shellcheck-py`）は、
本家から独立した個人または別組織のメンテに依存するため本体依存に組み込まない。
本家公式であってもインストール時に外部バイナリを取得するパッケージ（例: `editorconfig-checker`）は、
オフライン・プロキシ環境での導入失敗リスクを避けるため本体依存から除外する。
Node.js等のランタイムを伴うパッケージも、ランタイム導入とサプライチェーンの広さの観点から本体依存に含めない。

ネイティブバイナリツール（cargo系・dotnet系を含む）は`pyfltr/command/runner.py`の`_BIN_TOOL_SPEC`に
mise backend付きで登録する。
あわせて`pyfltr/config/config.py`の`{command}-runner`既定値を`"bin-runner"`に揃える。
グローバル`bin-runner`既定値`"mise"`へ委譲することで、追加ツール導入時もmise経由の自動セットアップが既定動作となる。
利用者は`{command}-runner = "direct"`または`{command}-path`の明示で個別に上書きできる。

`[python]` extrasは空配列エイリアスとして残す。
本体依存にPython系ツール一式が同梱されているため、`uv add --dev "pyfltr[python]"`でも
`uv add --dev pyfltr`でも利用者プロジェクトのvenvに同じものが入る。
extrasの空エイリアスは過去版からの利用者環境の`pyfltr[python]`指定をエラー化させない互換維持と、
将来Python系ツールの依存配置を見直す際の表記予約のために維持する。
推奨表記はドキュメント側で`uv add --dev "pyfltr[python]"`を採用し、
利用者に提示する表記を将来の方針変更に対しても安定させる。

## 呼び出し方の推奨

詳細は`docs/guide/recommended.md`の「呼び出し方の使い分け」節を参照する。

## ツール解決の方針

`{command}-runner`は2分類の値を取り、両者は対等な選択肢として並ぶ。

- カテゴリ委譲値: `python-runner` / `js-runner` / `bin-runner`。
  各カテゴリのグローバル設定値へ委譲する。
  Python系tool・JS系tool・ネイティブ系toolの`{command}-runner`既定値はそれぞれ対応するカテゴリ委譲値とする
- 直接指定値: `direct` / `mise` / `uv` / `uvx` / `pnpx` / `pnpm` / `npm` / `npx` / `yarn`。
  per-toolで実装ツールを直接指定する場合に使う

直接指定値のカテゴリ横断バリデーション（例: `mypy-runner = "pnpm"`）は拒否しない。
無意味な組み合わせは実行時に解決ロジックがエラー終了する（実装簡潔さ優先）。

各値の解決経路と各runner（カテゴリ委譲値・直接指定値）の優先順位は
`pyfltr/command/runner.py`の`build_commandline`のdocstringに集約する。
runner値体系（許容値・既定値）の網羅は`pyfltr/config/config.py`の`DEFAULT_CONFIG`冒頭docstringを参照する。

ツール解決経路の追跡情報は次の3系統で確認できる。

- JSONL header（`uv.lock`・`uv.available`・`uv.x_available`）: プロセス全体のuv経路前提条件
- JSONL commandレコード: fallback発生時のみ`effective_runner`・`runner_source`・`runner_fallback`を出力する。
  通常経路では省略しトークン消費を抑える
- `pyfltr command-info <command>`: 通常経路を含む詳細な解決状態（runner・effective_runner・mise/uv診断）を取得する

JSONLレコードでfallbackを能動通知し、必要時にcommand-infoで詳細確認するという責務分担とする。
JSONLフィールドの追加・名称変更は[.claude/skills/output-format/SKILL.md](../output-format/SKILL.md)に従う。

## pnpx経路でのplugin解決workaround

pnpx経路で`{command}-packages`によりplugin/rule packageを並べるとき、
pnpx prefix先頭に`--config.enableGlobalVirtualStore=false`を付与する。
pnpm 11対応のための対症療法で、pnpm 10では未知キーとして無視されるため後方互換性を保つ。
背景（pnpm 11既定変更・textlintのrequire解決失敗）と適用条件は
`pyfltr/command/runner.py`の`_resolve_js_commandline`内コメントに集約する。

## `~`展開の対象キーと適用タイミング

利用者ホームディレクトリ依存のパス（例: `~/dotfiles/.../tool.py`）を設定値として記述できるよう、
特定のper-toolキーに限り`~`展開を適用する。
対象キー範囲・適用タイミング・展開規則のSSOTは`pyfltr/config/config.py`の
`EXPAND_USER_KEY_SUFFIXES`定数のdocstringに集約する。
