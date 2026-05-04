# 利用者向けガイド

## 対応ツール

対応ツールを言語・用途別に示す。
言語カテゴリ（Python / JS/TS / Rust / .NET）に属するツールはすべて既定で無効（opt-in）。
`preset = "latest"` + 言語カテゴリキー（`python` / `javascript` / `rust` / `dotnet`）の`true`指定だけで、
当該言語の推奨ツール一式がゲートを通過して有効化される。
追加ツールや個別の無効化が必要な場合のみ`{command} = true` / `{command} = false`を書き足す。
詳細は[設定項目](configuration.md)を参照。

### Python系

対応するPython系ツールはruff-format / uv-sort / pylint / mypy / ruff-check / pyright / ty / pytestの8種。
このうちtyのみpreset非収録のため、必要に応じて`ty = true`を個別指定する。
Python系ツール一式は本体依存に同梱されているため、`uvx pyfltr`単発で利用できる。

- Formatters: ruff format / uv-sort（依存定義のソート）
- Linters: pylint / mypy / ruff check / pyright / ty
- Testers: pytest

### JS/TS系

対象はprettier / tsc / eslint / biome / oxlint / vitestの6種（TypeScriptも同カテゴリ）。
`js-runner`設定で起動方式（pnpx / pnpm / npx等）を切り替える。

- Formatters: prettier
- Linters: tsc（型チェック。`pass-filenames = false`でプロジェクト全体を対象）/ eslint / biome / oxlint
- Testers: vitest

### Rust系

推奨ツール一式はcargo-fmt / cargo-clippy / cargo-check / cargo-test / cargo-denyの5種。
プロジェクト全体（crate単位）を対象とし、`{command}-runner`既定値`"bin-runner"`に従い
グローバル`bin-runner`既定`"mise"`によりmise経由で起動する。
PATH上の`cargo`等を直接実行したい場合は`cargo-fmt-runner = "direct"`等を設定する。

- Formatters: cargo fmt
- Linters: cargo clippy / cargo check / cargo deny（依存ライセンス・脆弱性チェック）
- Testers: cargo test

### .NET系

推奨ツール一式はdotnet-format / dotnet-build / dotnet-testの3種。
プロジェクト全体（solution単位）を対象とし、`{command}-runner`既定値`"bin-runner"`に従い
グローバル`bin-runner`既定`"mise"`によりmise経由で起動する。
PATH上の`dotnet`を直接実行したい場合は`dotnet-format-runner = "direct"`等を設定する。
direct実行時は環境変数`DOTNET_ROOT`配下に`dotnet`実行ファイルがあれば優先採用する。

- Formatters: dotnet format
- Linters: dotnet build（ビルドエラーをlint段階で検出）
- Testers: dotnet test

### ドキュメント系

- Linters: markdownlint-cli2 / textlint

pyfltrの設定キーとコマンド名は`markdownlint`（例: `markdownlint = true`、`--commands=markdownlint`）だが、
実際に起動するのは`markdownlint-cli2`である。
これは設定キー名の簡潔さを優先した意図的な設計であり、利用者はこの対応関係を把握した上で設定・コマンド指定をする。

### その他

- Formatters: shfmt（既定で無効）/ taplo（TOML formatter、既定で無効）
- Linters
    - 一般: typos（PyPI依存）/ actionlint / ec（editorconfig-checker、既定で無効）/
      shellcheck（既定で無効）/ glab-ci-lint（既定で無効）
    - YAML / Dockerfile / シークレット系: yamllint（既定で無効）/ hadolint（Dockerfile、既定で無効）/
      gitleaks（シークレット検出、既定で無効）
- 統合: pre-commit（`.pre-commit-config.yaml`のhookを統合実行）

既定で無効（opt-in）のツールは、利用時に`pyproject.toml`で`{command} = true`を設定する。
特記事項を以下に示す。

- `taplo`: Rust製のTOMLフォーマッター/リンター。bin-runner経由で実行し、shfmtと同様の2段階実行（check→format）を行う
- `yamllint`: Python製のYAMLリンター。PATH上または`yamllint-path`で指定した実行ファイルを直接呼び出す
- `hadolint`: Dockerfileに特化したリンター。bin-runner経由で実行する
- `gitleaks`: Goバイナリのシークレット検出ツール。`gitleaks detect`でリポジトリ全体を対象に実行する
- `glab-ci-lint`: `glab ci lint`経由でGitLab CI設定を構文検証する。
  GitLab API認証とネットワーク接続が必須なため、CIや初学者環境で誤って失敗しないよう既定で無効化している

プリセット指定と言語カテゴリゲートによる有効化の詳細は[設定項目](configuration.md)を参照。

個別に有効化・無効化する方法や`python-runner`/`js-runner`/`bin-runner`などの補助設定は[設定項目（ツール別）](configuration-tools.md)を参照。

## コンセプト

- 各種ツールをまとめて並列で呼び出し、実行時間を短縮する
- 各種ツールのバージョンには極力依存しない（各ツール固有の設定には対応しない）
- excludeの指定方法が各ツールで異なる問題を、pyfltr側で解決してツールに渡すことで吸収する
- formatterはファイルを修正しつつエラーとしても扱う（`pyfltr ci`ではformatterによる変更も失敗と判定する）
- 設定は極力`pyproject.toml`に集約する

## インストール

推奨は`uvx`での実行。事前のインストールやdev依存への追加は不要で、常に最新のpyfltrを利用できる。

```shell
uvx pyfltr --help
```

`uv`でバージョン管理したい場合は`uv add --dev "pyfltr[python]"`で追加し、`uv run pyfltr ...`で呼び出す。
pip環境では`pip install pyfltr`を使う。

## ガイドページ

### CLI基本

- [CLIコマンド](usage.md) — サブコマンド・オプション・コーディングエージェント連携
- [トラブルシューティング](troubleshooting.md) — よくある問題と回避策

### 設定

- [設定項目](configuration.md) — 基本設定・プリセット・言語カテゴリゲート・並列実行
- [設定項目（ツール別）](configuration-tools.md) — ツール別の起動方式・2段階実行・カスタムコマンド

### 推奨構成

- [推奨設定例](recommended.md) — Pythonプロジェクト・タスクランナー・CI設定例
- [推奨設定例（非Pythonプロジェクト）](recommended-nonpython.md) — TypeScript/JS・Rust・.NETプロジェクト
- [カスタムコマンド例](custom-commands.md) — カスタムコマンドの設定例
