# 対応ツール

pyfltrが対応するformatter / linter / testerを言語・用途別に示す。
初めて使う場合は[はじめに](getting-started.md)を参照。設定から実行までの導入手順を確認できる。

言語カテゴリ（Python / JS/TS / Rust / .NET）に属するツールはすべて既定で無効（opt-in）。
`preset = "latest"` + 言語カテゴリキー（`python` / `javascript` / `rust` / `dotnet`）の`true`指定だけで、
当該言語の推奨ツール一式がゲートを通過して有効化される。
追加ツールや個別の無効化が必要な場合のみ`{command} = true` / `{command} = false`を書き足す。
詳細は[設定項目](configuration.md)を参照。

## Python系

対応するPython系ツールはruff-format / uv-sort / pylint / mypy / ruff-check / pyright / ty / pytestの8種。
このうちtyのみpreset非収録のため、必要に応じて`ty = true`を個別指定する。
Python系ツール一式は本体依存に同梱されているため、`uvx pyfltr`単発で利用できる。

- Formatters: ruff format / uv-sort（依存定義のソート）
- Linters: pylint / mypy / ruff check / pyright / ty
- Testers: pytest

## JS/TS系

対象はprettier / tsc / eslint / biome / oxlint / vitestの6種（TypeScriptも同カテゴリ）。
`js-runner`設定で起動方式（pnpx / pnpm / npx等）を切り替える。

- Formatters: prettier
- Linters: tsc（型チェック。`pass-filenames = false`でプロジェクト全体を対象）/ eslint / biome / oxlint
- Testers: vitest

## Rust系

推奨ツール一式はcargo-fmt / cargo-clippy / cargo-check / cargo-test / cargo-denyの5種。
プロジェクト全体（crate単位）を対象とし、`{command}-runner`既定値`"bin-runner"`に従い
グローバル`bin-runner`既定`"mise"`によりmise経由で起動する。
PATH上の`cargo`等を直接実行したい場合は`cargo-fmt-runner = "direct"`等を設定する。

- Formatters: cargo fmt
- Linters: cargo clippy / cargo check / cargo deny（依存ライセンス・脆弱性チェック）
- Testers: cargo test

## .NET系

推奨ツール一式はdotnet-format / dotnet-build / dotnet-testの3種。
プロジェクト全体（solution単位）を対象とし、`{command}-runner`既定値`"bin-runner"`に従い
グローバル`bin-runner`既定`"mise"`によりmise経由で起動する。
PATH上の`dotnet`を直接実行したい場合は`dotnet-format-runner = "direct"`等を設定する。
direct実行時は環境変数`DOTNET_ROOT`配下に`dotnet`実行ファイルがあれば優先採用する。

- Formatters: dotnet format
- Linters: dotnet build（ビルドエラーをlint段階で検出）
- Testers: dotnet test

## ドキュメント系

- Linters: markdownlint-cli2 / textlint / designmd / lychee

pyfltrの設定キーとコマンド名は`markdownlint`（例: `markdownlint = true`、`--commands=markdownlint`）だが、
実際に起動するのは`markdownlint-cli2`である。
これは設定キー名の簡潔さを優先した意図的な設計であり、利用者はこの対応関係を把握した上で設定・コマンド指定をする。

`designmd`の設定キーとコマンド名（例: `designmd = true`、`--commands=designmd`）は
内部識別子であり、実際に起動するnpmパッケージは`@google/design.md`である。
`@google/design.md`はpyproject.tomlのドット区切りキーと衝突するため、pyfltrは内部識別子として`designmd`を採用する。
対象ファイル名は仕様上`DESIGN.md`固定で、本リポジトリ内に該当ファイルがあれば自動的に対象となる。

`lychee`はRust製のリンク切れチェッカーで、Markdown・HTML中の外部URL到達性を検証する。
bin-runner経由（既定はmise）で起動する。
ネットワーク到達失敗による判定変動を抑えたい場合は`lychee-severity = "warning"`で警告扱いに切り替えられる。

## その他

- Formatters: shfmt（既定で無効）/ taplo（TOML formatter、既定で無効）
- Linters
    - 一般: typos（PyPI依存）/ actionlint / ec（editorconfig-checker、既定で無効）/
      shellcheck（既定で無効）/ glab-ci-lint（既定で無効）
    - YAML / Dockerfile / シークレット系: yamllint（既定で無効）/ hadolint（Dockerfile、既定で無効）
    - シークレット検出・SAST: gitleaks（既定で無効）/ semgrep（既定で無効）
    - SQL: sqlfluff（既定で無効）
- 統合: pre-commit（`.pre-commit-config.yaml`のhookを統合実行）

既定で無効（opt-in）のツールは、利用時に`pyproject.toml`で`{command} = true`を設定する。
特記事項を以下に示す。

- `taplo`: Rust製のTOMLフォーマッター/リンター。bin-runner経由で実行し、shfmtと同様の2段階実行（check→format）を行う
- `yamllint`: Python製のYAMLリンター。PATH上または`yamllint-path`で指定した実行ファイルを直接呼び出す
- `hadolint`: Dockerfileに特化したリンター。bin-runner経由で実行する
- `gitleaks`: Goバイナリのシークレット検出ツール。`gitleaks detect`でリポジトリ全体を対象に実行する
- `semgrep`: Python製の多言語SAST。ルールセット指定が必須のため既定で無効。
  利用時は`semgrep-args = ["scan", "--json", "--error", "--config=auto"]`等で実際のルールセットを指定する
- `sqlfluff`: Python製のSQL専用linter。dialect指定が必須のため`.sqlfluff`配置を前提とする。
  `sqlfluff lint`サブコマンドをlinterとして起動する（`sqlfluff format`サブコマンドは対象外）
- `glab-ci-lint`: `glab ci lint`経由でGitLab CI設定を構文検証する。
  GitLab API認証とネットワーク接続が必須なため、CIや初学者環境で誤って失敗しないよう既定で無効化している

プリセット指定と言語カテゴリゲートによる有効化の詳細は[設定項目](configuration.md)を参照。

個別に有効化・無効化する方法や`python-runner`/`js-runner`/`bin-runner`などの補助設定は
[設定項目（ツール別）](configuration-tools.md)を参照。

## 検索・置換機能

pyfltrは横断検索（`grep`）と置換（`replace`）も内蔵する。
pyfltr設定の`exclude`/`extend-exclude`/`respect-gitignore`を尊重するため、
`node_modules`や`build`配下のノイズが混入しない。
詳細は[検索と置換](grep-replace.md)を参照。

## コンセプト

- 各種ツールをまとめて並列で呼び出し、実行時間を短縮する
- 各種ツールのバージョンには極力依存しない（各ツール固有の設定には対応しない）
- excludeの指定方法が各ツールで異なる問題を、pyfltr側で解決してツールに渡すことで吸収する
- formatterはファイルを修正しつつエラーとしても扱う（`pyfltr ci`ではformatterによる変更も失敗と判定する）
- 設定は極力`pyproject.toml`に集約する
