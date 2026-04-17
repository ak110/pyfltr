# JSONL 出力拡張

v3.0.0パートCでJSONL出力の診断品質・利用性を底上げした変更点の設計記録。
LLMエージェント・CIシステムがpyfltrの診断結果を扱いやすくすることを狙う。

## 対象

- severity表記の正規化
- `rule_url` の自動付与
- `retry_command` の埋め込み
- smart truncation（ツール単位の診断数・メッセージ長の上限制御）
- SARIF 2.1.0 / GitHub Annotation形式への出力対応

## severity の 3 値統一

すべての診断の `severity` は `"error"` / `"warning"` / `"info"` のいずれかに正規化する。
各ツールの生値は `_normalize_severity()` で3値にマップする。
マップ例: mypyの `error`、pylintの `convention`、shellcheckの `STYLE` など。
未対応値は `None` としてフィールドごと省略する。
severityを持たないツール（mypy・markdownlint・tyなど）は従来どおり `None` を維持する。

## rule_url

`rule_url` は当該ruleの公式ドキュメントURLを指す文字列で、`diagnostic` レコードに条件付きで出力される。
対応ツールとURL組み立て方針は次の通り。

- `ruff-check`: JSON出力の `url` フィールドを最優先。無い場合は `https://docs.astral.sh/ruff/rules/{code}/`
- `pylint`: `https://pylint.readthedocs.io/en/stable/user_guide/messages/{category}/{symbol}.html`
    - `rule` には `symbol` (`missing-module-docstring` 等) を格納し、`message` に `messageId` を前置して `"C0114: Missing module docstring"` の形で保持する
    - `category` はpylint JSONの `type` フィールド (`convention` / `refactor` / `warning` / `error` / `fatal` / `information`) から決定する
- `pyright`: `https://microsoft.github.io/pyright/#/configuration?id={rule}`
- `mypy`: `https://mypy.readthedocs.io/en/stable/_refs.html#code-{rule}`
- `shellcheck`: `https://www.shellcheck.net/wiki/{rule}`（`rule` は `SC2086` 形式）
- `eslint`: `https://eslint.org/docs/latest/rules/{rule}`（プラグインルール `plugin/rule` はURL無し）
- `markdownlint`: `https://github.com/DavidAnson/markdownlint/blob/main/doc/{rule}.md`
- `textlint`: プラグイン間でURL体系が統一されていないため未対応

実装は `pyfltr/rule_urls.py` のテンプレート関数群を辞書ディスパッチで呼び分ける。
各カスタムパーサーは `build_rule_url(command=..., rule=..., existing_url=..., category=...)` を呼び、`ErrorLocation.rule_url` に格納する。
ビルトイン正規表現経路でも、mypyの末尾 `[error-code]` とmarkdownlintの先頭 `MDxxx` を名前付きグループ `rule` で抽出し、同じ関数でURLを補完する。

## retry_command

`retry_command` は当該ツール1件を再実行するためのshellコマンド文字列で、`tool` レコードに埋め込む。
`pyfltr/main.py` が `run_pipeline` 呼び出し時点で次を構築する。

- 起動プレフィックス: Linuxでは `/proc/self/status` 経由で親プロセスを辿り決定する。
    例えば `uv run` 経由なら `uv run pyfltr`、`uvx` 経由なら `uvx pyfltr` を採用する。
    macOSやWindowsなど親プロセスを取得できない環境では `sys.argv[0]` のbasenameにフォールバックする
- ベーステンプレート: 起動時の `sys.argv[1:]` をコピーして保持する。
    `--commands` 値を当該ツールへ差し替え、位置引数（ターゲット）は除去する
- ターゲット: 当該 `CommandResult.target_files` を絶対パス化して末尾に追加する。
    `--work-dir` 適用前の元cwdを基準とすることで、再実行時のcwd二重解釈を避ける

このため `pyfltr ci` 失敗時のretry_commandに `pyfltr run` が混入してfixステージが暴発することは無い。
`--no-fix` / `--output-format` / `--output-file` / `--exit-zero-*` / `--exclude` などの実行意味論フラグも保持される。

`pass-filenames=False` のツールでは `commandline` にファイル引数が含まれない。
tsc・cargo系・dotnet系などが該当する。
このため `CommandResult` に `target_files: list[pathlib.Path]` フィールドを追加した。
`execute_command()` がツール実行時のターゲットリストをそのまま埋める。

## smart truncation

JSONL側で次の上限を適用する。`pyproject.toml` で調整可能。

| 設定キー | 既定値 | 意味 |
| --- | --- | --- |
| `jsonl-diagnostic-limit` | `0` (無制限) | 1 ツールあたりの diagnostic 出力件数上限 |
| `jsonl-message-max-lines` | `30` | `tool.message` (生出力末尾) の行数上限 |
| `jsonl-message-max-chars` | `2000` | `tool.message` の文字数上限 |

切り詰めが発生した場合は `tool` レコードに `truncated` サブオブジェクトを添付する。

- `diagnostics_total`: 切り詰め前の総件数（diagnostic切り詰め時のみ）
- `lines`: 切り詰め前の行数（メッセージ切り詰め時のみ）
- `chars`: 切り詰め前の文字数（メッセージ切り詰め時のみ）
- `archive`: 全文の参照パス (`tools/<tool>/output.log` または `tools/<tool>/diagnostics.jsonl`)

切り詰めの可否は当該 `CommandResult.archived` フラグで判定する。
`archived=True`（アーカイブ書き込み成功）のときのみ切り詰めを適用する。
`archived=False` のときは切り詰めをスキップして全文をJSONLに出力する（復元不能な情報欠落の防止）。
`archived=False` になる具体例はアーカイブ無効・初期化失敗・個別ツールの書き込み失敗など。
判定単位はfixステージと通常ステージを区別する必要があるためCommandResult単位とする。
`_archive_hook()` が `write_tool_result()` 成功時に `CommandResult.archived = True` を立てる。

## SARIF 2.1.0 出力

`--output-format=sarif` でSARIF 2.1.0準拠のJSONをstdout（または `--output-file`）へ書き出す。
1ツール = 1 runオブジェクトとして対応付け、`tool.driver.rules` に重複なしで `rule` と `helpUri` を登録し、`results` 配列にdiagnosticを配置する。

severityからSARIF levelへの変換は次の通り。

- `error` → `"error"`
- `warning` → `"warning"`
- `info` → `"note"`
- 未設定 → `"warning"`（フォールバック）

`retry_command` は `invocations[].commandLine` に、`run_id` / `exit_code` / `commands` / `files` は `properties.pyfltr` に格納する。

## GitHub Annotation 出力

`--output-format=github-annotations` でGitHub Actionsの注釈形式 (`::error file=...`) を出力する。
GitHub Actionsが拾ってプル要求のファイル行にインライン表示する用途。

severity → ディレクティブのマップ。

- `error` → `::error`
- `warning` → `::warning`
- `info` → `::notice`
- 未設定 → `::warning`

`file` / `line` / `col` / `title` をプロパティとして付与し、本文はGitHub仕様に沿って `%` / 改行をパーセントエンコードする。
`title` は `{tool}: {rule}` 形式でruleが無ければtool名のみを使う。

## 構造化出力と stdout の占有

`--output-format` が `jsonl` / `sarif` / `github-annotations` のいずれかかつ `--output-file` 未指定のとき、pyfltrはstdoutを構造化出力のみで占有する。
実装は `_force_structured_stdout_mode()` と `_suppress_logging()` の組で行う。
jsonl以外も含む構造化出力に適用するため、従来のjsonl限定ヘルパーから改称・一般化した。

## 関連ファイル

- `pyfltr/rule_urls.py`: rule_urlテンプレート関数の辞書ディスパッチ
- `pyfltr/sarif_output.py`: SARIF 2.1.0ビルダー
- `pyfltr/github_annotations.py`: GitHub Annotation行ジェネレーター
- `pyfltr/error_parser.py`: severity正規化と各パーサーでのrule_url埋め込み
- `pyfltr/llm_output.py`: smart truncationと `truncated` メタ付与・rule_url・retry_commandのレコード反映
- `pyfltr/command.py`: `CommandResult.target_files` / `archived` / `retry_command` フィールド追加
- `pyfltr/main.py`: `_detect_launcher_prefix()` / `_build_retry_args_template()` / `_build_retry_command()` ・新 `--output-format` 分岐
- `pyfltr/config.py`: `jsonl-*` 設定キー追加
- `pyfltr/archive.py`: `_error_to_dict()` に `rule_url` を追加してdiagnostics.jsonlへ保存
