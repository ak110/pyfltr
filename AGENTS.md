# AGENTS.md: pyfltr

多言語プロジェクトの品質チェックを一元管理し、コーディングエージェントから扱える形で提供する基盤。
組み込みのformatter・linter・testerとプロジェクト固有のカスタムチェックを同じ手順で実行できる。
JSON Lines出力（`--output-format=jsonl`）とMCPサーバー（`pyfltr mcp`）を利用できる。

## 開発手順

- `make update`: 依存更新 + prek autoupdate + pinactアクション更新 + 全テスト実行
  - `make update-actions`: GitHub Actionsのハッシュピン更新のみ（mise経由でpinact実行）
- リリース手順: `gh workflow run release.yaml --field=bump=PATCH`（`PATCH`は`MINOR`・`MAJOR`に変更可）
- Docker再ビルド単発起動: `gh workflow run docker-build.yaml`
  - `ghcr.io/ak110/pyfltr:latest`をリリースを伴わず更新する
  - `--field=version=X.Y.Z`で特定バージョンを指定する。未指定時はPyPI最新公開版を採用する
- テストコードは`pyfltr/xxx_.py`に対して`tests/xxx_test.py`として配置する
- コミット前の検証方法: `make test`
  - formatter・linter・tester・カスタムチェックなど対応ツール全般を個別に直接起動せず、
    `make test`または`uv run pyfltr run <path>`を使う。
    設定で無効化しているツールも直接起動すれば動作するため、設定による無効化は直接起動への防御にならない
  - 特定ファイルのみを対象にする場合は`uv run pyfltr run <path>`にパスを渡す
  - 修正後の再実行時は`--commands=mypy,ruff-check`等で限定して実行する（最終検証はCIに委ねる前提）
  - エージェント検出用の環境変数が設定された環境では`run`の出力形式が`jsonl`、
    静音モードが既定で有効になるため、`run-for-agent`を明示指定する必要はない

## アーキテクチャの参照先

サブパッケージ・モジュールごとの構成詳細とサブパッケージ間の依存方向、
format別のlogger stream/level切替の詳細は[docs/development/architecture.md](docs/development/architecture.md)を参照する。

## 実装上の不変条件

- `subproject_aware=True`ツールはサブプロジェクトループの内側で動く前提で実装する。
  ツール起動時のcwdは`ExecutionContext.subproject_cwd`（指定時）または起点cwdを採用する
- subprocess・git・mise・ファイル走査などcwd依存処理はプロセスのcwdに依存しない実装にする。
  `subprocess.Popen(cwd=...)`の引数、または`start_cwd`・`base_cwd`・`cwd`等の
  明示引数でcwdを渡す。`os.chdir()`でグローバル状態を変更しない
- `pyfltr/command/core_.py`の`ExecutionParams.targets`と`CommandResult.target_files`は
  実際に処理対象となったファイル集合を表すフィールドである。
  マスク済み一時パスなど値の意味を変える差し替えをしない。
  一時パスへの差し替えが必要な用途は`ExecutionParams.cache_commandline`と
  `ExecutionParams.file_path_remap`で表現する
  （両フィールドの下流経路は[docs/development/architecture.md](docs/development/architecture.md)を参照する）
- サブプロジェクトごとに繰り返す処理が警告を発行する場合は、
  起点実行分を含めて同一の`source`と`message`の組が1件に収まることを確認する。
  モノレポ構成での実測手順は[docs/development/architecture.md](docs/development/architecture.md)の
  「モノレポ対応」節を参照する

## 注意点

- 対象ファイルに応じて`.claude/skills/`配下のpyfltr固有スキル
  （`test-constraints`・`output-format`・`tool-resolution`・`ssot`・
  `grep-replace`・`pyfltr-add-tool`）を呼び出す
- `pyfltr/command/error_parser.py`を変更した場合、`error-parser-reviewer`サブエージェントによる
  網羅レビューをコミット前に完了させる。事後レビューにすると不良を含むコミットが記録され、
  是正に追加のリリースを要する
- `pyfltr/colloquial/words.txt`は冒頭コメントでコーディングエージェントによる読み込みを禁じているため、
  運用方針コメントの更新ではファイル全体を読み込む編集手段を使わず、
  `sed -i '<コメント終端行>a<挿入行>' <パス>`のように行番号を指定した挿入で編集する
  - コメント終端行は`grep -vn '^#' <パス> | head -1 | cut -d: -f1`が返す行番号から1を引いた値とする
  - 変更の確認では`sed -n '1,<コメント終端行>p' <パス>`でコメント範囲へ限定して表示し、
    `grep -c '' <パス>`で行数を照合する。辞書エントリー本体は表示させない
