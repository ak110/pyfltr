# CLIコマンド

## サブコマンド

pyfltrはサブコマンドで動作モードを指定する。
v3.0.0でサブコマンドは必須化された（省略時のフォールバックは廃止）。

```shell
pyfltr <subcommand> [files and/or directories ...]
```

### サブコマンド: ci

```shell
pyfltr ci [files and/or directories ...]
```

全チェック実行。CI環境やコミット前の検証に適する。

終了コード:

- 0: Formattersによるファイル変更が無く、かつLinters/Testersでのエラーも無い場合
- 1: 上記以外の場合

### サブコマンド: run

```shell
pyfltr run [files and/or directories ...]
```

全チェック実行。
Formattersによるファイル変更があってもLinters/Testersでのエラー無しなら終了コードは0になる。
ローカルでの全チェック実行に適する。

### サブコマンド: run-for-agent

```shell
pyfltr run-for-agent [files and/or directories ...]
```

`run`と同じ動作をLLMエージェント向けJSONL出力（`--output-format=jsonl`）で実行するエイリアス。
`pyfltr run --output-format=jsonl`と等価で、Claude Codeなどのエージェントから呼び出す際に用いる。

出力形式の詳細は[jsonl形式の使い方](#jsonl)を参照。

### サブコマンド: fast

```shell
pyfltr fast [files and/or directories ...]
```

pre-commitフックなどで実行しても作業に支障が出にくい高速なコマンドだけを実行する軽量チェック。
mypy / pylint / pytestなど起動やファイルあたりの処理に時間がかかるコマンドは除外される。
Formattersによるファイル変更があっても終了コードは0になる。

既定で含まれるコマンドは以下。

- Formatters: `ruff-format` `prettier` `uv-sort` `shfmt` `cargo-fmt` `dotnet-format`
- Linters: `ec` `shellcheck` `typos` `actionlint` `ruff-check` `ty` `markdownlint` `textlint` `biome` `oxlint` `cargo-clippy`
- その他: `pre-commit`（`.pre-commit-config.yaml`のhookを統合実行）

含まれるコマンドは各コマンドの`{command}-fast`設定で制御できる（[設定](configuration.md)を参照）。

### サブコマンド: generate-config

```shell
pyfltr generate-config
```

設定ファイルの雛形を標準出力に書き出す。`[tool.pyfltr]`セクションに貼り付けて利用する。
このサブコマンドは他のオプションやターゲット指定を受け付けず、設定出力だけを行う。

### サブコマンド: list-runs {#list-runs}

```shell
pyfltr list-runs [--limit N] [--output-format text|json|jsonl]
```

実行アーカイブに保存されたrun一覧を新しい順で表示する。

- 既定で直近20件（`--limit`で変更可能）
- `text`: 固定幅テーブル。列は`RUN_ID` / `STARTED_AT` / `EXIT` / `FILES` / `COMMANDS`
- `json`: `{"runs":[{...}, ...]}`の単発出力
- `jsonl`: 1件1行ストリーム（`kind: "run"`）

アーカイブ未作成の環境では`(no runs)`を出力し、終了コードは0。

### サブコマンド: show-run {#show-run}

```shell
pyfltr show-run <run_id> [--tool NAME] [--output] [--output-format text|json|jsonl]
```

指定runの詳細を表示する。

- `<run_id>`: ULID完全一致のほか、先頭一意な前方一致と`latest`エイリアスを受け付ける。前方一致で複数該当した場合は曖昧エラー（終了コード1）
- 既定: `meta`（`run_id`・`started_at`・`finished_at`・`exit_code`・`files`・`commands`）とツール別サマリ（`status` / `has_error` / `diagnostics`）を表示
- `--tool NAME`: 指定ツールの`tool.json`と`diagnostics.jsonl`全件を表示
- `--tool NAME --output`: 指定ツールの生出力（`output.log`）全文を表示
- `--output-format`: `text`（行形式 `key: value`）・ `json`（単発dict）・ `jsonl`（`kind: "meta"` / `"tool"` / `"diagnostic"` / `"output"` 種別の1行1レコード）

存在しない`run_id`・`--tool`指定時は終了コード1で標準エラーにメッセージを出力する。

同じツールが`fix`ステージと通常ステージの両方で実行された場合、アーカイブの保存キーはツール名固定のため通常ステージ側の結果で上書きされる（`show-run`で参照できるのは各ツールの最終保存結果のみ）。

### サブコマンド: mcp

```shell
pyfltr mcp
```

stdioトランスポートでMCPサーバーを起動する。
追加オプションはなく、起動後はstdin/stdoutをJSON-RPCフレームが専有する。MCPクライアントがstdinを閉じた時点でサーバーが終了する。

提供するMCPツール（5件）:

| ツール名 | 対応CLI | 説明 |
| --- | --- | --- |
| `list_runs` | `pyfltr list-runs` | run一覧を新しい順で返す。`limit`で件数制御（既定20件） |
| `show_run` | `pyfltr show-run <run_id>` | 指定runのmetaとツール別サマリを返す。前方一致・`latest`エイリアス可 |
| `show_run_diagnostics` | `pyfltr show-run <run_id> --tool <name>` | 指定runのtool.jsonとdiagnostics全件を返す |
| `show_run_output` | `pyfltr show-run <run_id> --tool <name> --output` | 指定runのoutput.log全文を返す |
| `run_for_agent` | `pyfltr run-for-agent` | 指定パスにlint/format/testを実行しrun_id・終了コード・失敗ツール名を返す |

`run_for_agent`ツールの引数:

- `paths`: 実行対象のファイルまたはディレクトリのパス一覧（必須）
- `commands`: 実行するコマンド名のリスト（省略時はプロジェクト設定の全コマンドを使用）
- `fail_fast`: `true`の場合、1ツールでもエラーが発生した時点で残りを打ち切る（既定`false`）

MCPクライアント（Claude Desktopなど）からの設定例:

```json
{
  "mcpServers": {
    "pyfltr": {
      "command": "pyfltr",
      "args": ["mcp"]
    }
  }
}
```

`uv run`経由で起動する場合:

```json
{
  "mcpServers": {
    "pyfltr": {
      "command": "uv",
      "args": ["run", "pyfltr", "mcp"]
    }
  }
}
```

### サブコマンド: generate-shell-completion

```shell
pyfltr generate-shell-completion bash
pyfltr generate-shell-completion powershell
```

シェル補完スクリプトを標準出力に書き出す。
引数にシェル種別（`bash`または`powershell`）を指定する。

bashでの設定例:

```shell
eval "$(pyfltr generate-shell-completion bash)"
```

PowerShellでの設定例:

```powershell
pyfltr generate-shell-completion powershell | Out-String | Invoke-Expression
```

永続化する場合はプロファイルに上記を追記する。

### `[files and/or directories ...]`

対象を指定しなかった場合は、カレントディレクトリ(`.`)を指定した場合と同じ扱いとなる。

指定したファイルやディレクトリの配下のうち、各コマンドのtargetsパターンに一致するファイルのみ処理される。
一例を以下に示す。

- Python系ツール: `*.py`
- markdownlint / textlint: `*.md`
- pytest: `*_test.py`

### `fast` / `run` / `run-for-agent` / `ci`の動作の違いと自動修正（fixステージ）

各サブコマンドの主な違いを以下に示す（軽い順）。

| 項目 | `fast` | `run` | `run-for-agent` | `ci` |
| --- | --- | --- | --- | --- |
| 対象コマンド | `{command}-fast = true`のツールのみ | 有効な全ツール | 有効な全ツール | 有効な全ツール |
| fixステージ（自動修正） | 有効 | 有効 | 有効 | 無効 |
| Formatterによる変更時の終了コード | `0`（成功扱い） | `0`（成功扱い） | `0`（成功扱い） | `1`（失敗扱い） |
| Linters / Testersのエラー時の終了コード | `1` | `1` | `1` | `1` |
| 既定の出力形式 | `text` | `text` | `jsonl` | `text` |
| 主な用途 | pre-commitフックなどの軽量チェック | ローカルでの全チェック実行 | LLMエージェントからの呼び出し | CI・コミット前チェック |

`fast` / `run` / `run-for-agent` サブコマンドは、formatter段の前にfixステージを内蔵する。

fixステージでは`{command}-fix-args`が定義された有効なlinterを`--fix`付きで順次実行する。
対象ツールは`ruff-check` / `textlint` / `markdownlint` / `eslint` / `biome` / `cargo-clippy`など。
`ruff check --fix` → `ruff format` → `ruff check`のような2段階処理をpyfltrのパイプライン全体で実現する位置づけ。

カスタムコマンドでも`pyproject.toml`の`[tool.pyfltr.custom-commands.<name>]`に`fix-args = [...]`を定義すればfixステージの対象になる。

## 特定のツールのみ実行

```shell
pyfltr ci --commands=ruff-check,markdownlint [files and/or directories ...]
```

カンマ区切りで実行するツールだけ指定する。全サブコマンドで使用可能。

以下のエイリアスも使用可能。(例: `--commands=format`)

- `format`: `pre-commit` `ruff-format` `prettier` `uv-sort` `shfmt` `cargo-fmt` `dotnet-format`
- `lint`:
    - Python系: `ruff-check` `mypy` `pylint` `pyright` `ty`
    - Markdown系: `markdownlint` `textlint`
    - JS/TS系: `eslint` `biome` `oxlint` `tsc`
    - Rust系: `cargo-clippy` `cargo-check` `cargo-deny`
    - .NET系: `dotnet-build`
    - その他: `ec` `shellcheck` `typos` `actionlint`
- `test`: `pytest` `vitest` `cargo-test` `dotnet-test`
- `fast`: per-commandの`{cmd}-fast`フラグがtrueのコマンド

※ `pyproject.toml`の`[tool.pyfltr]`で無効になっているコマンドは無視される。

## UI

ターミナル上で実行すると、TextualベースのTUIが自動的に有効になる。

- Summaryタブ: 各コマンドのステータス・エラー数・経過時間をリアルタイム表示
- Errorsタブ: エラー発生時のみ出現し、全コマンドのエラー箇所を`ファイル:行番号`形式で一覧表示
- 各コマンドタブ: コマンドの出力をリアルタイム表示

Errorsタブのエラー一覧は`ファイル:行番号: [コマンド名] メッセージ`形式で、
VSCodeのターミナルからクリックして該当箇所にジャンプできる。

- `--ui`: UIを強制的に有効化する（非対話端末など自動的にUIが無効になる環境でも起動する）
- `--no-ui`: UIを無効化し、出力を直接ターミナルに表示（エラー一覧の後にサマリーを表示）
- `--stream`: 非TUIモード時に各コマンドの完了時点で即時出力する（既定は全コマンド完了後にまとめて出力）
- `--no-exclude`: exclude/extend-excludeパターンによるファイル除外を無効化する
- `--no-gitignore`: `.gitignore`によるファイル除外を無効化する
- `--no-archive`: 実行アーカイブ（ユーザーキャッシュ配下への全実行の保存）を無効化する
- `--no-cache`: ファイルhashキャッシュ（対象ファイル未変更時の再実行スキップ）を無効化する
- `--fail-fast`: 1ツールでもエラーが発生した時点で残りのジョブを打ち切る（起動済みサブプロセスには `terminate()` を送り、未開始ジョブは `skipped` として扱われる）
- `--only-failed`: 直前runの実行アーカイブから失敗ツール・失敗ファイルを抽出し、ツール別にその組み合わせだけを再実行する。直前runが無い・失敗ツールが無い・指定 `targets` との交差が空の場合はメッセージを出して `rc=0` で成功終了する。診断ファイルが取得できないツール（pytest等の `pass-filenames=False` 系）は既定ファイル展開にフォールバックして全体再実行する。`--no-archive` とは独立に働く（参照するのは過去runのアーカイブ）
- `--from-run <RUN_ID>`: `--only-failed` の参照対象runを明示指定する（前方一致・`latest`対応）。未指定時は直前runを自動選択。`--only-failed` との併用が前提で、単独指定はargparseエラーで拒否する。指定した`<RUN_ID>`が存在しない場合は警告を出して `rc=0` で早期終了する
- `--ci`: CI環境向け（`--no-shuffle --no-ui` 相当）
- `-j N` / `--jobs N`: linters/testersの最大並列数を指定（既定: 4、`pyproject.toml`でも設定可能）
- `--verbose`: デバッグレベルのログを出力する
- `--keep-ui`: TUI終了後にTextual画面を保持する（ログ確認用）
- `--work-dir DIR`: pyfltrの作業ディレクトリを指定する（既定はカレントディレクトリ）

その他のオプションは `pyfltr --help` を参照。

## 出力形式

`--output-format`で出力形式を切り替えられる。

| 値 | 用途 | 動作 |
| --- | --- | --- |
| `text` | 既定。人間向け・従来互換 | logging経由で進捗・詳細・summaryをまとめて表示 |
| `jsonl` | LLMエージェント向け | JSON Lines形式で診断・ツール結果・全体集計を出力 |
| `sarif` | CIツール連携向け | SARIF 2.1.0形式のJSONを1件出力 |
| `github-annotations` | GitHub Actions向け | `::error file=...`形式の注釈行を出力 |

`sarif` / `github-annotations` は全結果集約後に1回だけ書き出すため、ストリーミング出力は無い。
`jsonl` と同様、stdout指定時（`--output-file`未指定）は`text`ログが完全に抑止される。

### jsonl形式の使い方 {#jsonl}

```shell
pyfltr run --output-format=jsonl
# 以下は上記と等価のエイリアス
pyfltr run-for-agent
```

`--output-format=jsonl`かつ`--output-file`未指定時、stdoutにはJSONLのみを書き、既存の`text`ログ（進捗・詳細・summary）は完全に抑止される。TUIや`--stream`、`--ui`も暗黙に無効化される。

`--output-file=path`を指定するとJSONLはファイルへ書き出され、stdoutには従来どおりの`text`出力が並行して出る（ローカル実行時も開発者が進捗を追える）。

環境変数`PYFLTR_OUTPUT_FORMAT`でも出力形式の既定値を指定できる（値は`text`または`jsonl`）。
CLIオプション`--output-format`が指定されている場合は環境変数より優先される。
エージェント起動スクリプトなどに`PYFLTR_OUTPUT_FORMAT=jsonl`を設定しておけば、毎回オプションを明示しなくても`ci`など任意のサブコマンドでJSONL出力に切り替えられる。

### jsonlスキーマ

出力は以下5種別のレコードからなる。`kind`フィールドでレコード種別を判別する。

- `header`: 先頭1行。実行環境情報（`version` / `run_id` / `python` / `platform` / `cwd` / `commands` / `files` / `schema_hints`）
- `warning`: pyfltrが検出した設定・実行時の警告（`source`で発生元を識別）
- `diagnostic`: 個々の診断（`error_parser`対応ツールの抽出結果）
- `tool`: 1ツール1レコードの実行メタ情報
- `summary`: 最終1行、全体集計。`failed > 0`のとき英語の`guidance`配列を付与する

v3.0.0以降、`header`レコードには`run_id`（ULID）が含まれる。実行アーカイブが有効な場合のみ付与され、[`pyfltr show-run`](#show-run) / [`pyfltr list-runs`](#list-runs)で該当runの詳細を参照できる。

```json
{"kind":"header","version":"3.0.0","run_id":"01HXYZ...","python":"3.12.0 ...","executable":"/usr/bin/python3","platform":"linux","cwd":"/work","commands":["mypy","ruff-format"],"files":12}
{"kind":"warning","source":"config","msg":"pre-commit が有効化されていますが、設定ファイルが見つかりません: .pre-commit-config.yaml"}
{"kind":"diagnostic","tool":"mypy","file":"src/a.py","line":42,"col":5,"msg":"Incompatible return value type"}
{"kind":"tool","tool":"mypy","type":"linter","status":"failed","files":12,"elapsed":0.8,"diagnostics":1,"rc":1}
{"kind":"tool","tool":"ruff-format","type":"formatter","status":"formatted","files":12,"elapsed":0.3,"diagnostics":0,"rc":1}
{"kind":"summary","total":2,"succeeded":0,"formatted":1,"failed":1,"skipped":0,"diagnostics":1,"exit":1}
```

stdoutモード（`--output-file`未指定）では、先頭にheader行を出力し、各ツールの完了時にdiagnostic行+tool行を随時書き出す。
ツール間の出力順は完了順となり、最後にwarning行+summary行が続く。
ファイル出力時（`--output-file`指定）では、先頭にheader行、続いて`pyproject.toml`の定義順にツール単位でグルーピングし、先頭にwarning行、末尾にsummary行を配置する。

`warning`レコードの`source`は`config`（設定ファイル不在など）/`tool-resolve`（ツール解決失敗）/`file-resolver`（対象ファイル選定時）/`git`（`git check-ignore`失敗）のいずれか。

`diagnostic`レコードの`col`は抽出できた場合のみ含まれる。`tool`レコードの`rc`は`returncode is not None`のときのみ含まれる（`skipped`では省略）。

`diagnostic`レコードの任意フィールド:

- `rule`: ルールコード (`F401` / `missing-module-docstring` / `SC2086` 等)。取得できたツールでのみ出力
- `rule_url`: ルール公式ドキュメントのURL。対応ツール（ruff / pylint / pyright / mypy / shellcheck / eslint / markdownlint）でのみ出力
- `severity`: `"error"` / `"warning"` / `"info"` の3値に正規化。未対応ツールは省略
- `fix`: 自動修正の可能性。値は `"safe"` / `"unsafe"` / `"suggested"` / `"none"` のいずれか。ツールが自動修正情報を返さない場合はフィールドごと省略

`tool`レコードは`status == "failed"`かつ`diagnostics == 0`のときに限り、`message`フィールドに`CommandResult.output`の末尾をトリムした内容を含める。実行ファイル未検出など、`error_parser`でパースできない失敗理由を捕捉するため。
トリム閾値は`jsonl-message-max-lines`（既定30行）と`jsonl-message-max-chars`（既定2000文字）で調整できる。

`tool`レコードの任意フィールド:

- `retry_command`: 当該ツール1件を再実行するshellコマンド文字列。
    失敗時（`status == "failed"`）のみ出力され、成功時・`cached=true` の結果では出力されない（再実行動機がないため）。
    `--no-fix` / `--output-format` などの実行意味論フラグを保持し、ターゲットを「当該ツールで失敗したファイルのみ」に絞り込む（v3.0.0以降、パートG）。
    失敗ファイルが特定できない場合（pytest等の `pass-filenames=False` や全体失敗のみ）はターゲット位置引数が空になる（ツール単体の再実行文字列として機能する）
- `truncated`: smart truncationが発生した場合のみ付与。
    `diagnostics_total`（切り詰め前の総件数）・`lines` / `chars`（メッセージの元サイズ）・`archive`（全文参照パス）を含む
- `cached`: ファイルhashキャッシュから結果を復元した場合のみ `true`。現状は `textlint` のみ対象
- `cached_from`: `cached=true` の場合に付与される復元元 `run_id`（ULID）。`show-run` やMCP経由で当該runの全文を参照できる

smart truncationは次の設定キーで制御する（`pyproject.toml`）。

- `jsonl-diagnostic-limit`: 1ツールあたりのdiagnostic出力件数上限（既定 `0` = 無制限）
- `jsonl-message-max-lines` / `jsonl-message-max-chars`: `tool.message`の行数・文字数上限

切り詰めはアーカイブ書き込みに成功したツールのみに適用され、`--no-archive` や書き込み失敗時は全文を出力する（復元不能な情報欠落の防止）。

`summary`レコードの`diagnostics`キーは全ツール合算の診断数で、個別の`tool`レコードの`diagnostics`と集計名を統一している。

`header.schema_hints`と`summary.guidance`はLLMエージェントへのメタ情報を提供する（どちらも英語）。

- `header.schema_hints`: JSONLの各フィールドの意味を短い英文で補足する辞書。毎runに同梱されるため、LLM側で事前知識がなくてもJSONLを解釈できる
- `summary.guidance`: `failed > 0`のときだけ付与される英語の配列。`tool.retry_command`の参照、`pyfltr run-for-agent --only-failed`の活用、`diagnostic.fix`値の解釈、`pyfltr show-run <run_id>`の案内を並べる

### LLM連携の例

`Claude Code`などで`pyfltr`の結果を参照する場合、jsonl形式が扱いやすい。

ファイル末尾の`summary`行を読めば全体像を把握でき、必要に応じて`diagnostic`行を参照することでトークン消費を抑えられる。

LLMエージェントがpyfltrを活用する基本的な流れ:

1. 全体実行でsummaryを確認する

    ```shell
    pyfltr run-for-agent
    ```

    末尾のsummary行（`"kind":"summary"`）で`failed`の有無と`diagnostics`数を確認し、問題がなければ完了する。

2. 問題があるツール/ファイルだけ個別に再実行する

    ```shell
    pyfltr run-for-agent --commands=mypy path/to/file.py
    ```

    `--commands`で特定ツールに絞ることで出力量を抑えつつ、`diagnostic`行から修正対象のファイル・行番号・メッセージを取得する。
    詳細が必要な場合に限り`run`で再実行するなど、段階的に情報を掘り下げることも可能。

## pre-commitとの統合

pyfltrは`.pre-commit-hooks.yaml`を同梱していない。
pre-commitから呼び出したい場合は、`.pre-commit-config.yaml`の`repo: local`でlocal hookとして`uv run pyfltr`または`uvx pyfltr`を呼び出す。
pyfltrの実行方式をプロジェクトのuv環境と揃えられるため、依存管理・バージョン固定の観点で一元化できる。

### Pythonプロジェクト（pyfltrを`uv.lock`に含める構成）

pyfltr自身のリポジトリを含むPython系プロジェクトで採用している構成。
`uv run --frozen pyfltr fast`を`uv.lock`ごとバージョン固定し、pre-commit時にキャッシュ済み`.venv`を再利用する。

```yaml
repos:
  - repo: local
    hooks:
      - id: pyfltr-fast
        name: pyfltr fast
        language: system
        entry: uv run --frozen pyfltr fast
        require_serial: true
        types: [file]
```

### 非Pythonプロジェクト（`uvx`で都度取得する構成）

Rust / .NETなどpyfltrを`uv.lock`に含めないプロジェクトでは`uvx pyfltr fast`を直接呼び出す。
`mise`などを用いて`uv`を導入する手順にしておけば、チームメンバー間で環境差異が出にくい。

```yaml
repos:
  - repo: local
    hooks:
      - id: pyfltr-fast
        name: pyfltr fast
        language: system
        entry: uvx pyfltr fast
        require_serial: true
        types: [file]
```

### 共通の注意点

- `pyfltr fast` はfixステージを内蔵する。pre-commit hookから`{command}-fix-args`定義済みlinter（`cargo-clippy` / `ruff-check` / `textlint`等）の自動修正が走るため、別hookを並べる必要は無い
- formatter（`ruff-format` / `prettier` / `cargo-fmt` / `dotnet-format`等）は通常実行で常時書き込みモードで動作するため、fixステージでは扱わない
- `pass-filenames = False`のツール（`cargo-*` / `dotnet-*` / `tsc`等）はcrate / solution全体を対象とするため、コミット時に未変更ファイルまで書き換わる可能性がある。cargo系・dotnet系は`serial_group`で自動直列化されるので、利用者が`--jobs=1`などを指定する必要は無い
