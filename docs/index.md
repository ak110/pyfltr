# pyfltr

多言語プロジェクトの品質チェックを一元管理し、コーディングエージェントから扱える形で提供する基盤。
Python / Rust / .NET / TypeScript・JavaScript / ドキュメントなどの
formatter・linter・testerを単一コマンドで実行する。
プロジェクト固有のカスタムチェックも統合できる。

## ドキュメントの読み方

- [はじめに](guide/getting-started.md): インストールから設定・実行までの導入手順
- [対応ツール](guide/index.md): 言語・用途別の対応ツール一覧
- [開発者向けガイド](development/development.md): 開発環境構築・ドキュメント運用・リリース手順

## コンセプト

- 組み込み機能とプロジェクト固有の処理を同じ設定・実行・出力の規則で扱う
- 人間とコーディングエージェントが同じ品質チェック結果を利用できる形式で提供する
- 各種ツールをまとめて並列で呼び出し、実行時間を短縮する
- 各種ツールのバージョンには極力依存しない（各ツール固有の設定には対応しない）
- excludeの指定方法が各ツールで異なる問題を、pyfltr側で解決してツールに渡すことで吸収する
- 起点ディレクトリ外のファイルもチェックできる（[CLIコマンド](guide/usage.md)の「外部パス指定時の挙動」）
- formatterはファイルを修正しつつエラーとしても扱う（`pyfltr ci`ではformatterによる変更も失敗と判定する）
- 設定は極力`pyproject.toml`に集約する
- `pyfltr fast`はpytestなど重いツールを含めない。変更ファイルに依らず毎回確かめたいテストは`pytest-fast-targets`で対象を指定してfastへ含められる（[設定](guide/configuration.md#pytest-fast-targets)）

## プロジェクト固有のカスタムチェック

独自スクリプトや社内ツールなどは、組み込みツールと同じ実行基盤へ統合できる。
設定方法と実行時の扱いは[プロジェクト固有チェックの追加](guide/custom-commands.md)を参照。

## 検索・置換機能

pyfltrは横断検索（`grep`）と置換（`replace`）も内蔵する。
pyfltr設定の`exclude`/`extend-exclude`/`respect-gitignore`を尊重するため、
`node_modules`や`build`配下のノイズが混入しない。
詳細は[検索と置換](guide/grep-replace.md)を参照。
