# トラブルシューティング

## MCP起動時のstdout占有事故

`pyfltr mcp`起動後に他のコマンド出力やlogが端末に表示されず、
コーディングエージェントに返る結果が文字化けするまたは解析エラーになる場合がある。

`pyfltr mcp`を起動するとstdin/stdoutがJSON-RPCフレームに専有される。
同一プロセスや同一パイプライン内で他のテキスト出力が混入すると、
JSON-RPCパーサーがフレームを正しく解析できなくなる。

回避策。

- `pyfltr mcp`は単独プロセスとして起動し、他のコマンドとパイプで繋がない
- コーディングエージェントのMCP設定では`command`に`pyfltr`または`uv run pyfltr`だけを指定し、
  ラッパースクリプトを経由する場合はstdoutに余分な出力が混入しないことを確認する
- デバッグ目的でlogを確認したい場合はstderrにリダイレクトする
 （pyfltrはtextout・system logを常にstderrに出力するため、stderrは参照できる）

## pre-commit統合時の自動スキップ

pre-commitからpyfltrを呼び出しているのに、一部のツールが実行されないことがある。
これはpre-commit経由起動時の意図的な絞り込み動作である。

pyfltrはpre-commitから呼び出されたことを環境変数`PRE_COMMIT=1`で検出する。
`PRE_COMMIT=1`が設定されている場合、`pyfltr fast`サブコマンドは`{command}-fast = true`のツールのみに絞って実行する。
`run`サブコマンドは自動スキップを行わないため、`fast`を指定している場合は意図した動作である。

確認方法。

- pre-commitの`entry`設定が`pyfltr fast`になっているか確認する
- `{command}-fast`の設定を`pyproject.toml`で確認する
 （既定では重いツール、mypy・pylint・pytestなどはfastに含まれない）
- `pyfltr fast --verbose`で実行対象コマンドの一覧を確認する

## 実行アーカイブのディスク使用量確認（定期管理）

pyfltrは各実行の結果をユーザーキャッシュ配下にアーカイブとして保存する。
長期間使用するとディスク使用量が増加する場合があるため、定期的な確認手順を以下に示す。

`pyfltr list-runs`で実行アーカイブの一覧を確認できる。

```shell
# 直近20件を一覧表示（既定）
pyfltr list-runs

# 件数を増やして確認
pyfltr list-runs --limit 100

# JSONL形式で詳細確認
pyfltr list-runs --output-format=jsonl
```

一覧には`RUN_ID`・`STARTED_AT`・`EXIT`・`FILES`・`COMMANDS`が表示される。
アーカイブが存在しない環境では`(no runs)`を出力する。

アーカイブの実体はユーザーキャッシュディレクトリ（Linux: `~/.cache/pyfltr/`、
Windows: `%LOCALAPPDATA%\pyfltr\`）に保存される。
手動で削除する場合はそのディレクトリを対象にする。

アーカイブを無効化したい場合は`--no-archive`オプションまたは
`pyproject.toml`の`archive = false`設定を使う。
