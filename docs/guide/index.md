# 利用者向けガイド

## 対応ツール

対応ツールを言語・用途別に示す。
言語カテゴリ（Python / JS/TS / Rust / .NET）に属するツールはすべて既定で無効（opt-in）。
`preset = "latest"` + 言語カテゴリキー（`python` / `javascript` / `rust` / `dotnet`）の`true`指定だけで、
当該言語の推奨ツール一式がゲートを通過して有効化される。
追加ツール（`ty`など）や個別の無効化が必要な場合のみ`{command} = true` / `{command} = false`を書き足す。
詳細は[設定項目](configuration.md)を参照。

### Python系

対象はruff-format / ruff-check / mypy / pylint / pyright / pytest / uv-sortの7種。
`ty`はプリセット非収録のため、必要な場合のみ個別に`ty = true`を指定する。
同時に`pip install pyfltr[python]`でPython系ツールの依存を追加する必要がある。

- Formatters: ruff format / uv-sort（依存定義のソート）
- Linters: ruff check / mypy / pylint / pyright / ty
- Testers: pytest

### JS/TS系

対象はeslint / biome / oxlint / prettier / tsc / vitestの6種（TypeScriptも同カテゴリ）。
`js-runner`設定で起動方式（pnpx / pnpm / npx等）を切り替える。

- Formatters: prettier
- Linters: eslint / biome / oxlint / tsc（型チェック。`pass-filenames = false`でプロジェクト全体を対象）
- Testers: vitest

### Rust系

推奨ツール一式はcargo-fmt / cargo-clippy / cargo-check / cargo-test / cargo-denyの5種。
プロジェクト全体（crate単位）を対象に直接実行する（`{command}-path`で実行パスを指定）。

- Formatters: cargo fmt
- Linters: cargo clippy / cargo check / cargo deny（依存ライセンス・脆弱性チェック）
- Testers: cargo test

### .NET系

推奨ツール一式はdotnet-format / dotnet-build / dotnet-testの3種。
プロジェクト全体（solution単位）を対象に直接実行する（`{command}-path`で実行パスを指定）。

- Formatters: dotnet format
- Linters: dotnet build（ビルドエラーをlint段階で検出）
- Testers: dotnet test

### ドキュメント系

- Linters: markdownlint-cli2 / textlint

### その他

- Formatters: shfmt（既定で無効）
- Linters: ec（editorconfig-checker、既定で無効）/ shellcheck（既定で無効）/ typos（PyPI依存）/
  actionlint / glab-ci-lint（既定で無効）
- 統合: pre-commit（`.pre-commit-config.yaml`のhookを統合実行）

`glab-ci-lint`は`glab ci lint`経由でGitLab CI設定を構文検証する。
GitLab API認証とネットワーク接続が必須なため、CIや初学者環境で誤って失敗しないよう既定で無効化している。

プリセット指定と言語カテゴリゲートによる有効化の詳細は[設定項目](configuration.md)を参照。

個別に有効化・無効化する方法や`bin-runner`/`js-runner`などの補助設定は[設定項目（ツール別）](configuration-tools.md)を参照。

## コンセプト

- 各種ツールをまとめて並列で呼び出し、実行時間を短縮する
- 各種ツールのバージョンには極力依存しない（各ツール固有の設定には対応しない）
- excludeの指定方法が各ツールで異なる問題を、pyfltr側で解決してツールに渡すことで吸収する
- formatterはファイルを修正しつつエラーとしても扱う（`pyfltr ci`ではformatterによる変更も失敗と判定する）
- 設定は極力`pyproject.toml`に集約する

## インストール

```shell
pip install pyfltr
```

## ガイドページ

- [CLIコマンド](usage.md) — CLIの使い方・サブコマンド・オプション
- [設定項目](configuration.md) — 基本設定・プリセット・並列実行
- [設定項目（ツール別）](configuration-tools.md) —
  ツール別設定（直接実行 / js-runner / bin-runnerのカテゴリ別設定・2段階実行・カスタムコマンド）
- [推奨設定例](recommended.md) — 推奨設定（Pythonプロジェクト・タスクランナー・CI）
- [推奨設定例（非Pythonプロジェクト）](recommended-nonpython.md) — 非Pythonプロジェクトの推奨設定
- [カスタムコマンド例](custom-commands.md) — カスタムコマンドの設定例
- [v3.0.0マイグレーションガイド](migration-v3.md) — v2.xからv3.0.0への移行手順
