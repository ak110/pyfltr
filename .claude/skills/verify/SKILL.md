---
name: verify
description: pyfltrのCLIとMCPを隔離した一時プロジェクトで起動し、変更後の実際の操作と副作用を確かめる。
---

# pyfltrの実行時検証

## 起動と状態確認

リポジトリルートで、証拠を保存できる一時領域の下に、まだ存在しない絶対パスを選ぶ。
以下の `<証拠ディレクトリ>` を置き換えて実行する。
操作ごとに別のパスを指定する。

```bash
uv run --frozen python .claude/skills/verify/probe.py run "<証拠ディレクトリ>"
uv run --frozen python .claude/skills/verify/probe.py mcp-replace "<証拠ディレクトリ>"
```

主入口はCLI（`python -m pyfltr` はインストールされた `pyfltr` と同じ入口）。
別入口のMCPは `pyfltr mcp` と等価な起動引数でstdioへ接続する。
補助スクリプトは渡された証拠先の隣に空のプロジェクトを作り、`PYFLTR_CACHE_DIR` と `PYFLTR_GLOBAL_CONFIG` をその内側へ固定する。
実ユーザーのデータや設定を読み込まない。
サンプルとローカルのPythonを使うチェックを投入するので、追加の対応ツールやポートは要らない。

`state.json` のモジュールパスと `PYTHONPATH` が検証対象のworktreeであり、キャッシュと設定が一時プロジェクトの下にあることを確認する。
全操作で `--version.json` のバージョンと終了コードも確認する。
MCPは `tests/cli/mcp_transport_test.py` と同じく改行区切りJSON-RPCで `initialize` に応答した後に `notifications/initialized` を送る。
ツール能力がなければ操作へ進まない。

## 操作と観測

コマンド例の `run` の位置へ操作名を渡す。

| 操作名 | 入口と確認内容 |
| --- | --- |
| `run` | CLIの通常チェック。JSONLの結果と、実際のチェックが作った `checked` の内容を確認する。 |
| `ci` | CLIのCIモード。修正を伴わないローカルチェックを使い、コマンドの結果と `checked` を確認する。自動修正の検証には修正を行う別の入力が必要。 |
| `fast` | CLIの短縮チェック。設定したカスタムチェックは `fast = true` が必要。結果と `checked` を確認する。 |
| `grep` | CLIでサンプルの `before` を検索し、出力に一致がありファイルは変化しないことを確認する。 |
| `replace` | CLIで `before` を `after` へ置換する。既定では実書き込みなので、終了コードだけでなくファイルの変更を確認する。 |
| `mcp-run` | MCPの `tools/call` で `run` を呼び、応答の `exit_code` と `checked` を確認する。 |
| `mcp-replace` | MCPの `tools/call` で `replace` を呼ぶ。`dry_run` 未指定ではプレビューなので、応答に置換後の内容があり、ファイルが変化しないことを確認する。実書き込みには `dry_run: false` が必要。CLIの結果では代用しない。 |

## 証拠と後始末

CLIの引数、終了コード、標準出力と標準エラーは操作名のJSONに残る。
MCPの送受信とサーバーの終了コードは `rpc.jsonl`、標準エラーは `server-stderr.log` に残る。
読み戻したファイルの内容は `observed.txt` に残る。
失敗時は既に書かれた証拠を読み、次回は別の証拠先で実行する。
実データや秘匿値を入力に使わない。

MCPの標準入力を閉じて自分が起動したサーバーの終了を待つ。
時間内に終了しなければそのプロセスを停止する。
一時プロジェクトは成功・失敗にかかわらず削除する。
成功時は `cleanup.txt` のパスが存在せず、証拠のファイルが引き続き読めることを確認する。
