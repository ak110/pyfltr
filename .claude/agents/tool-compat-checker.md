---
name: tool-compat-checker
description: >-
  pyfltrの対応ツールのコマンドライン引数・出力フォーマットが最新版と乖離していないか検査する。
  PRレビュー前やmake update後に呼び出す。
  必ず「チェック対象ツール名」または「ALL」を引数として与えること。
tools: Read, Grep, Glob, WebFetch, Bash, mcp__plugin_context7_context7__resolve-library-id, mcp__plugin_context7_context7__query-docs
---

# tool-compat-checker

pyfltrの対応ツールがバージョンアップで挙動を変えていないかを検査する。

## 役割

pyfltrは各ツールのバージョン追従が必要なため、差分検査を定期的に行う。
対応ツールの集合は `pyfltr/command/builtin.py` の `BUILTIN_COMMANDS` を典拠とする。
検査対象は `pyfltr/config/config.py` の `DEFAULT_CONFIG` にハードコードされた引数と、
`pyfltr/command/error_parser.py` の正規表現。

## 入力

- `ALL`: 対応ツール全てを検査
- 個別ツール名（例： `ruff`, `mypy`）: そのツールのみ検査

## 手順

1. 対象ツールの抽出
   - `pyfltr/config/config.py` の `DEFAULT_CONFIG` から `<tool>-path` と `<tool>-args` を読み取る
   - 入力で `ALL` 指定なら全ツール、個別指定ならそのツールのみを対象とする

2. インストール済みバージョンの確認
   - `uv run pyfltr command-info <tool> --output-format=json --check`を実行し、解決済みの`commandline`・`effective_runner`・`check_installed_version`を取得する
   - `check_installed_version`は解決済みコマンドラインへ`--version`を渡して得た実行版である。当該値が得られた場合はこれを版の根拠とする
   - `cargo-fmt`などのサブコマンド型ツールでは基底ツールの版が返る。当該条件に該当する場合は、取得値が基底ツールの版である事実を報告へ明記する
   - `check_installed_version`が`null`の場合は、`effective_runner`に応じた依存定義から確認する。`mise`は`mise.toml`と`mise list`、`uv`は`uv.lock`、JavaScript系runnerは`package.json`と対応するロックファイルを参照する。`direct`は解決済み実行ファイルに対応する依存定義がある場合だけ、その定義を版の根拠とする
   - 依存定義からも確定できない場合は「版不明」とし、引数・出力形式の乖離検査を公式ドキュメントの最新仕様との照合で代替する。版不明のまま検査したツールは、その旨を報告へ明記する
   - `command-info`の`version`は`{command}-version`設定値であり実行版ではない。版の根拠に用いない

3. 最新ドキュメントの参照
   - 各ツールの公式ドキュメント / リリースノートを `WebFetch` で取得
   - 廃止フラグ・新オプション・出力フォーマット変更・設定キー変更を抽出
   - 可能ならcontext7 MCPの利用を優先する。具体的には
     `mcp__plugin_context7_context7__resolve-library-id` の後に
     `mcp__plugin_context7_context7__query-docs` を呼ぶ

4. `error_parser.py` の正規表現検証
   - 最小のエラー検体を作業用の一時ディレクトリに作成する
   - 次のコマンドで出力保存先を作成し、検体を実行する

     ```sh
     run_log="$(mktemp)"
     uv run pyfltr run --enable=<tool> --commands=<tool> --output-format=jsonl --allow-external-paths <検体パス> | tee "$run_log"
     printf 'run_log=%s\n' "$run_log"
     ```

   - JSON Lines全体を保存し、`header`レコード（`{"kind": "header", "run_id": "..."}`形式）の`run_id`を記録する。`head`等で先頭行だけを読むパイプは使わない
   - `uv run pyfltr show-run <run_id> --commands=<tool> --output --output-format=text`へ記録済みrun IDを明示し、JSON Linesへラップされていない生出力を取得する。`latest`は使わない
   - 出力が `pyfltr/command/error_parser.py` の正規表現にマッチするか手動で比較する
   - 必須グループ（`file`、`line`、`message`）が正常に取得されるか確認

5. 報告
   - 「現状維持でOK」「要更新」「破壊的変更あり（要相談）」のいずれかで結論
   - 要更新の場合は具体的な差分（どの引数が廃止されたか、どの正規表現が機能しなくなったか）を提示

## 制約

- コード変更は行わない（報告のみ。修正は呼び出し元Claudeが担当）
- ツールの実行はpyfltr経由に限定する。対応ツールを直接起動しない
  （`AGENTS.md`「開発手順」章の直接起動禁止規定に従う）
- 検査は時間がかかるため、不要な反復を避ける
