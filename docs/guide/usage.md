# CLIコマンド

## サブコマンド

pyfltrはサブコマンドで動作モードを指定する。

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

`run`と同じ動作で出力形式の既定値を`jsonl`に切り替えたサブコマンド。
コーディングエージェントから呼び出す際に用いる。
`pyfltr run --output-format=jsonl`と多くの場合は等価だが、サブコマンド既定値であるため
`PYFLTR_OUTPUT_FORMAT=text`で`text`へ切り戻せる点が異なる。

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

### サブコマンド: config {#config}

```shell
pyfltr config <action> [options]
```

設定ファイルをCLIから操作する（pnpm/npm config互換の体系）。
`--global`なしはカレントディレクトリの`pyproject.toml`、`--global`付きはグローバル設定ファイル
（`~/.config/pyfltr/config.toml`など）が対象となる。

#### config get

```shell
pyfltr config get <key> [--global]
```

指定キーの現在値を1行出力する。ファイルに書かれていなければデフォルト値を返す。
未知のキーはエラー終了（終了コード1）。

```shell
pyfltr config get archive-max-age-days
pyfltr config get archive-max-age-days --global
```

#### config set

```shell
pyfltr config set <key> <value> [--global]
```

指定キーに値を書き込む。値の文字列はキーのデフォルト値の型に応じて変換される。

- `bool`: `true` / `false` / `1` / `0`
- `int`: 整数表記
- `str`: そのまま文字列として設定する
- `list[str]`: カンマ区切りで分割してリスト化する（例: `"foo,bar"` → `["foo", "bar"]`）
- `dict`: CLI非対応（エラー終了）

```shell
pyfltr config set archive-max-age-days 30 --global
pyfltr config set preset latest
```

`--global`指定時、グローバル設定ファイルが存在しなければディレクトリを含めて自動作成する。
project側（`--global`なし）で`pyproject.toml`が存在しない場合はエラー終了する。
未知のキーはエラー終了（終了コード1）。

`set`時の警告条件:

- archive/cache系のキー（`archive` / `archive-max-runs`等）をproject側にsetした場合:
  globalで集約することを推奨する旨の警告を出す。
- archive/cache以外のキーをglobal側にsetした場合:
  通常はproject側が優先されるため、globalで設定しても上書きされる旨の警告を出す。

#### config delete

```shell
pyfltr config delete <key> [--global]
```

設定ファイルから指定キーを削除する。
該当キーがなければ正常終了（何もしない）。
対象ファイルが存在しない場合は「削除対象がありません」と表示して正常終了する。
未知のキーはエラー終了（終了コード1）。

#### config list

```shell
pyfltr config list [--global] [--output-format text|json|jsonl]
```

設定ファイルに書かれているキーと値の一覧を表示する（デフォルト値は含まない）。

- `text`（既定）: `key = value`形式の行出力
- `json`: `{"values": {...}}`の単発出力
- `jsonl`: 1件1行の`{"key": ..., "value": ...}`ストリーム

設定項目の一覧とデフォルト値を確認したい場合は、まず`pyfltr config list`で現在の設定を確認し、
設定項目名の詳細は[設定項目一覧](configuration.md#config-keys)を参照。

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
pyfltr show-run <run_id> [--commands NAME[,NAME...]] [--output] [--output-format text|json|jsonl]
```

指定runの詳細を表示する。

- `<run_id>`: ULID完全一致のほか、先頭一意な前方一致と`latest`エイリアスを受け付ける。
  前方一致で複数該当した場合は曖昧エラー（終了コード1）
- 既定: `meta`（`run_id`・`started_at`・`finished_at`・`exit_code`・`files`・`commands`）と
  ツール別サマリ（`status` / `has_error` / `diagnostics`）を表示
- `--commands NAME[,NAME...]`: 指定ツールの`tool.json`と`diagnostics.jsonl`全件を表示。
  カンマ区切りで複数指定可（入力順で並ぶ）
- `--commands NAME --output`: 指定ツールの生出力（`output.log`）全文を表示（単一指定のみ）
- `--output-format`: `text`（行形式 `key: value`）・`json`（単発dict）・`jsonl`
 （`kind: "meta"` / `"command"` / `"diagnostic"` / `"output"` 種別の1行1レコード）

存在しない`run_id`・`--commands`指定時は終了コード1で標準エラーにメッセージを出力する。

同じツールが`fix`ステージと通常ステージの両方で実行された場合、アーカイブの保存キーはツール名固定のため
通常ステージ側の結果で上書きされる（`show-run`で参照できるのは各ツールの最終保存結果のみ）。

### サブコマンド: mcp

```shell
pyfltr mcp
```

stdioトランスポートでMCPサーバーを起動する。
追加オプションはなく、起動後はstdin/stdoutをJSON-RPCフレームが専有する。
MCPクライアントがstdinを閉じた時点でサーバーが終了する。

提供するMCPツール（5件）:

| ツール名 | 対応CLI | 説明 |
| --- | --- | --- |
| `list_runs` | `pyfltr list-runs` | run一覧を新しい順で返す。`limit`で件数制御（既定20件） |
| `show_run` | `pyfltr show-run <run_id>` | 指定runのmetaとツール別サマリを返す。前方一致・`latest`エイリアス可 |
| `show_run_diagnostics` | `pyfltr show-run <run_id> --commands <name>` | 指定runのtool.jsonとdiagnostics全件を返す（複数指定可） |
| `show_run_output` | `pyfltr show-run <run_id> --commands <name> --output` | 指定runのoutput.log全文を返す（単一指定のみ） |
| `run_for_agent` | `pyfltr run-for-agent` | lint/format/testを実行しrun_id・失敗ツール名・retry_commands等を返す |

`run_for_agent`ツールの引数:

- `paths`: 実行対象のファイルまたはディレクトリのパス一覧（必須）
- `commands`: 実行するコマンド名のリスト（省略時はプロジェクト設定の全コマンドを使用）
- `fail_fast`: `true`の場合、1ツールでもエラーが発生した時点で残りを打ち切る（既定`false`）
- `only_failed`: `true`の場合、直前runの失敗ツール・失敗ファイルのみ再実行する（CLIの`--only-failed`相当、既定`false`）
- `from_run`: `only_failed=true`の参照runを明示指定する（前方一致・`latest`可、`only_failed=true`のときのみ有効）

`run_for_agent`ツールの戻り値フィールド:

- `run_id`: 実行アーカイブの参照キー（ULID）。early exit時は `null`
- `exit_code`: 終了コード（`0` = 成功、`1` = 失敗）
- `failed`: 失敗したコマンド名の一覧
- `commands`: コマンド別サマリ一覧（`status`・`has_error`・`diagnostics` 件数）
- `skipped_reason`: early exitが発生した理由。
  `only_failed=false`の通常実行時は`null`で省略される。
  `only_failed=true`有効時に「直前runなし」「失敗ツールなし」「対象ファイル交差が空」の
  いずれかに該当した場合にのみ設定される
- `retry_commands`: 失敗コマンドの再実行シェルコマンド辞書（コマンド名→シェル文字列、成功・cachedは省略）

コーディングエージェント側へのMCPサーバー登録例（JSON形式で設定ファイルに記載する場合）:

```json
{
  "mcpServers": {
    "pyfltr": {
      "command": "uvx",
      "args": ["pyfltr", "mcp"]
    }
  }
}
```

エージェント常駐起動は独立venvで動くuvxの方がプロジェクトの`pyproject.toml`解釈やcwd依存を受けず安定する。

Claude Codeから登録する場合は`claude mcp add`コマンドが使える:

```shell
claude mcp add pyfltr -- uvx pyfltr mcp
```

### サブコマンド: command-info {#command-info}

```shell
pyfltr command-info <tool> [--format text|json] [--check]
```

対象ツールの起動方式（runner種別・実行ファイルパス・最終コマンドライン）の解決結果を副作用無しで表示する。
`pyproject.toml`の`{command}-runner`設定や`python-runner` / `js-runner` / `bin-runner`の影響を実環境で確認したいときに使う。

出力はセクション見出し（`## 実行コマンド` / `## ランナー解決` / `## mise診断` / `## 設定` / `## 環境変数`）で関連項目をまとめる。
情報が無いセクションは省略される。

mise設定（プロジェクトmise.tomlまたはグローバル設定）に`rust`記述がある場合の出力例:

```console
$ pyfltr command-info cargo-fmt
# cargo-fmt

## 実行コマンド

commandline: mise exec -- cargo fmt
executable: mise
executable_resolved: /home/user/.local/bin/mise

## ランナー解決

runner: bin-runner (default)
effective_runner: mise
mise_tool_spec_omitted: True

## mise診断

mise_active_tool_key: rust
mise_active_tools.status: ok
mise_active_tools.active_keys: rust

## 設定

enabled: True
configured_args: fmt
version: latest
```

tool specを省略した`mise exec -- cargo fmt`形になり、mise設定の解決済み内容（バージョン固定・components等）が反映される。
mise設定に`rust`記述が無い場合は`mise exec rust@latest -- cargo fmt`形になる。

uv診断系フィールドの意味は次の通り。
`{command}-runner`がuv経路に到達するツール（per-tool値が`python-runner` / `uv` / `uvx`）でのみ`## uv診断`セクションが出力される。

- `mode`: `uv`または`uvx`のいずれの経路で起動するかを示す解決後の値。
  `python-runner`委譲時はグローバル`python-runner`設定値（`uv` / `uvx`）に従う。
  `python-runner = "direct"`時など`uv`/`uvx`いずれにも該当しない場合は`## uv診断`セクション自体が出力されない
- `uv_available`: `uv`バイナリが利用可能かどうか
- `uvx_available`: `uvx`shimが利用可能かどうか
- `uv_lock_present`: cwdに`uv.lock`が存在するかどうか
- `direct_fallback`: 経路ごとの前提条件不足によりdirectフォールバックが発生したか
 （`mode == "uv"`では`uv`バイナリと`uv.lock`の両方が必要、`mode == "uvx"`では`uvx`shimの可用性のみで判定）
- `python_tool_bin`: コマンド名から解決された実際の実行ファイル名（`ruff-format`/`ruff-check`は`ruff`になる）

mise診断系フィールドの意味は次の通り。
出力は機械可読のテキスト形式で、JSON形式（`--format=json`）でも同じキーが取れる。

- `mise_tool_spec_omitted`: mise経路で`["exec", "--", <bin>]`形（tool spec省略形）を採用したか。
  commandline文字列の見た目に頼らずに判別できる
- `mise_active_tool_key`: mise active tools辞書を引く際の照合キー（`spec.mise_backend or spec.bin_name`）。
  mise.tomlに記述する際の名称ずれを事前に発見するために使う。mise backend未登録のツール（python系・js系）では出力しない
- `mise_active_tools.status`: `mise ls --current --json`の取得状況。
  値は`ok` / `mise-not-found` / `untrusted-no-side-effects` / `trust-failed` / `exec-error` /
  `json-parse-error` / `unexpected-shape`の7値
- `mise_active_tools.detail`: 取得失敗時のみ。mise stderrの先頭やexceptionメッセージを整形した1行
- `mise_active_tools.active_keys`: `status == "ok"`かつ活性化ツールが1つ以上ある場合のみ。
  mise設定が解決した活性化ツール名一覧。空の場合はテキスト出力では行ごと省略する

`--check`無しで`mise_active_tools.status`が`untrusted-no-side-effects`のときに限り、
trust試行を発動できる旨の1行案内が`hint:`プレフィックスで出る。
他のエラー要因では案内せず、ノイズを増やさない。

`{command}-fix-args`が定義されているコマンド（textlint・markdownlintなど）では、`commandline (fix step):`と`commandline (check step):`を併記する。
fix段とcheck段の二度実行が異なる引数を必要とするためである。

```console
$ pyfltr command-info textlint
# textlint

## 実行コマンド

commandline (fix step): pnpx --package textlint --package ... textlint --fix
commandline (check step): pnpx --package textlint --package ... textlint --format json
...
```

textlintの場合、fix段では`@textlint/fixer-formatter`が`compact`をサポートしない。
このためユーザーが指定した`--format`ペアを除去した形が表示される。
check段では`textlint-json`設定（既定`true`）により出力フォーマット指定`--format json`が注入される。

主要なオプション。

- `--format=text|json`: 出力形式を切り替える（既定`text`）。json形式はスクリプトからのパース向け
- `--check`: mise経由ツールに対して`mise exec --version`での事前チェックを行う
 （`mise install` / `mise trust`が発火する場合があるため、既定では行わない）

未知のコマンド名や`{command}-runner = "mise"`を未登録ツールに指定した場合などは終了コード1で失敗する。

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
- textlint / markdownlint: `*.md`
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
| 主な用途 | pre-commitフック等 | ローカルで全チェック | コーディングエージェント呼び出し | CI・コミット前 |

`fast` / `run` / `run-for-agent` サブコマンドは、formatter段の前にfixステージを内蔵する。

fixステージでは`{command}-fix-args`が定義された有効なlinterを`--fix`付きで順次実行する。
対象ツールは`ruff-check` / `textlint` / `markdownlint` / `eslint` / `biome` / `cargo-clippy`など。
`ruff check --fix` → `ruff format` → `ruff check`のような2段階処理をpyfltrのパイプライン全体で実現する位置づけ。

カスタムコマンドでも`pyproject.toml`の`[tool.pyfltr.custom-commands.<name>]`に`fix-args = [...]`を定義すれば
fixステージの対象になる。

## 特定のツールのみ実行

```shell
pyfltr ci --commands=ruff-check,markdownlint [files and/or directories ...]
```

カンマ区切りで実行するツールだけ指定する。全サブコマンドで使用可能。
`--commands`は複数回指定も可能で、カンマ区切りと併用できる。
例えば`--commands=mypy --commands=pyright,ruff-check`は`--commands=mypy,pyright,ruff-check`と同じになる。

以下のエイリアスも使用可能。(例: `--commands=format`)

- `format`: `pre-commit` `ruff-format` `prettier` `uv-sort` `shfmt` `cargo-fmt` `dotnet-format`
- `lint`:
    - Python系: `ruff-check` `mypy` `pylint` `pyright` `ty`（tyはpreset非収録のため未有効時はスキップ）
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
- `--fail-fast`: 1ツールでもエラーが発生した時点で残りのジョブを打ち切る
 （起動済みサブプロセスには`terminate()`を送り、未開始ジョブは`skipped`として扱われる）
- `--changed-since <REF>`: gitの任意のref（ブランチ・タグ・コミットハッシュ・`HEAD`など）からの変更ファイルのみを対象とする。
  `git diff --name-only <REF>`で取得したコミット差分・trackedファイルの作業ツリー差分・staged差分の和集合と
  展開済みファイル一覧の交差が対象となり、untrackedの新規ファイルは対象外。
  gitが不在またはrefが存在しない場合は警告を出して全体実行へフォールバックする。
  `--only-failed`と併用した場合は`--changed-since`フィルタを先に適用してから`--only-failed`フィルタを適用する
- `--only-failed`: 直前runの実行アーカイブから失敗ツール・失敗ファイルを抽出し、
  ツール別にその組み合わせだけを再実行する。直前runが無い・失敗ツールが無い・指定`targets`との交差が空の場合は
  メッセージを出して`rc=0`で成功終了する。診断ファイルが取得できないツール（pytest等の`pass-filenames=False`系）は
  既定ファイル展開にフォールバックして全体再実行する。`--no-archive`とは独立に働く（参照するのは過去runのアーカイブ）
- `--from-run <RUN_ID>`: `--only-failed`の参照対象runを明示指定する（前方一致・`latest`対応）。
  未指定時は直前runを自動選択。`--only-failed`との併用が前提で、単独指定はargparseエラーで拒否する。
  指定した`<RUN_ID>`が存在しない場合は警告を出して`rc=0`で早期終了する
- `--ci`: CI環境向け（`--no-shuffle --no-ui` 相当）
- `-j N` / `--jobs N`: linters/testersの最大並列数を指定（既定: 4、`pyproject.toml`でも設定可能）
- `--verbose`: デバッグレベルのログを出力する
- `--keep-ui`: TUI終了後にTextual画面を保持する（ログ確認用）
- `--work-dir DIR`: pyfltrの作業ディレクトリを指定する（既定はカレントディレクトリ）

TUI実行中にCtrl+Cを1秒以内に2回続けて押すと協調中断モードへ移行する。
実行中のサブプロセスを終了させ、完了済みツールの結果と中断された残りツール一覧をsummaryへ反映したうえで
終了コード130（`128 + SIGINT`）を返す。
中断時はwarnings欄に次の形式の1行が追加される。

```text
[pyfltr] Ctrl+C により中断しました。中断されたツール: <ツール名の一覧>
```

協調中断モードでさらにCtrl+Cを2回続けて押すと、後始末を待たず強制終了（終了コード130）となる。

その他のオプションは `pyfltr --help` を参照。

## 出力形式

`--output-format`で出力形式を切り替えられる。

| 値 | 用途 | 動作 |
| --- | --- | --- |
| `text` | 既定。人間向け・従来互換 | stdoutに進捗・詳細・summaryをまとめて表示 |
| `jsonl` | LLMエージェント向け | stdoutにJSON Lines形式で診断・ツール結果・全体集計を出力。text整形はstderrのWARN以上に抑止 |
| `sarif` | CIツール連携向け | stdoutにSARIF 2.1.0形式のJSONを1件出力。text整形はstderrのINFO |
| `github-annotations` | GitHub Actions向け | `text`と同じレイアウトをstdoutに出し、エラー箇所のみGAワークフローコマンド記法で補強 |
| `code-quality` | GitLab CI向け | stdoutにCode Climate JSON issue形式の配列を1件出力。text整形はstderrのINFO |

`jsonl`はツール完了順にストリーミング出力し、最後にwarning/summary行を追加する。
`sarif`と`code-quality`は全結果集約後に1回だけ書き出す。
いずれも`--output-file`未指定時はstdoutを占有し、text整形出力はstderrへ振り分けられる
（`jsonl`のみWARN以上に抑止）。
`github-annotations`はstdoutをtext整形が占有するため、stdout占有は起きない。

`--output-file`を指定した場合は、構造化データはファイルへ、stdoutには常に`text`整形出力が並行して出る。
`jsonl`はファイル出力時もストリーミング（ツール完了順）で書き出す。

text出力サマリー行で`status=formatted`のコマンドは末尾に`; no rerun needed`を付与する。
formatterによる書き換えはそれ自体が成功扱いで、再実行を要しないことを示す。

### jsonl形式の使い方 {#jsonl}

```shell
pyfltr run --output-format=jsonl
# 以下はサブコマンド既定値で出力形式を jsonl にする略形（PYFLTR_OUTPUT_FORMAT で text へ切り戻し可能）
pyfltr run-for-agent
```

`--output-format=jsonl`かつ`--output-file`未指定時、stdoutにはJSONLのみを書き、
text整形出力（進捗・詳細・summary）はstderrのWARN以上に抑止される。
TUIや`--stream`、`--ui`も暗黙に無効化される。

`--output-file=path`を指定するとJSONLはファイルへ書き出され、stdoutには従来どおりの`text`出力が並行して出る
（ローカル実行時も開発者が進捗を追える）。

環境変数`PYFLTR_OUTPUT_FORMAT`でも出力形式の既定値を指定できる
（値はサブコマンドが受理する出力形式のいずれか）。
CLIオプション`--output-format`が指定されている場合は環境変数より優先される。
エージェント起動スクリプトなどに`PYFLTR_OUTPUT_FORMAT=jsonl`を設定しておけば、
毎回オプションを明示しなくても`ci`など任意のサブコマンドでJSONL出力に切り替えられる。

環境変数`AI_AGENT`が設定されていれば、`--output-format`未指定時の既定値が`jsonl`になる
（コーディングエージェント環境下での自動切り替え用）。
値の中身は問わず、空文字列でない値が設定されていれば真扱いとなる
（`AI_AGENT=1`・`AI_AGENT=cursor`等いずれも有効）。

優先順位は`CLI > PYFLTR_OUTPUT_FORMAT > サブコマンド既定値（run-for-agent=jsonl）> AI_AGENT > text`。
`PYFLTR_OUTPUT_FORMAT`を明示すれば`AI_AGENT`環境下や`run-for-agent`配下でもtext等へ切り戻せる
（例: `PYFLTR_OUTPUT_FORMAT=text pyfltr run-for-agent`）。

### コーディングエージェント連携

コーディングエージェントから`pyfltr`を呼び出す方法は2種類ある。

#### 直接呼び出し（推奨）

エージェントがシェルコマンドを実行できる環境では、`pyfltr run-for-agent`を直接呼ぶ方法が最もシンプル。
JSONL出力をそのまま読み込める。

#### MCP経由

`pyfltr mcp`でMCPサーバーを起動すると、コーディングエージェントが`run_for_agent`ツールとして呼び出せる。
CLIの`run-for-agent`とは異なりJSONL出力がstdoutに流れないため、エージェントのMCPクライアントが結果を構造化データとして受け取れる。
ただし`pyfltr mcp`起動後は同一プロセスのstdin/stdoutがJSON-RPCに専有されるため、
他のコマンドと組み合わせた場合に出力が混ざる事故に注意する
（詳細は[トラブルシューティング](troubleshooting.md)を参照）。

コーディングエージェントが`pyfltr run-for-agent`を活用する基本的な流れ:

1. 全体実行でsummaryを確認する

    ```shell
    pyfltr run-for-agent
    ```

    末尾のsummary行（`"kind":"summary"`）の`commands_summary.needs_action`配下を見て対応要件数の有無を確認し、問題がなければ完了する。
    `commands_summary.needs_action`配下の`failed` / `resolution_failed`がいずれも0であれば残作業は無く、`commands_summary.no_issues`配下の内訳は確認不要。
    `applied_fixes`が非空でも`summary.guidance`に注記が出るが、formatter/fix-stageによる書き換えのみで再実行は不要なため、そのまま完了してよい。

2. 失敗したツール/ファイルだけ再実行する

    ```shell
    # 失敗ツールを --commands で絞る
    pyfltr run-for-agent --commands=mypy path/to/file.py

    # または直前runの失敗ツール・失敗ファイルをまとめて再実行
    pyfltr run-for-agent --only-failed
    ```

    `--commands`で特定ツールに絞ることで出力量を抑えつつ、`diagnostic`行から修正対象のファイル・行番号・メッセージを取得する。
    `command.retry_command`フィールドには当該ツールだけを失敗ファイルに絞り込んだ再実行コマンドが既に生成されているため、
    そのまま貼り付けて実行できる。
    `--only-failed`は直前runのアーカイブから失敗ツール・失敗ファイルを自動抽出して再実行する。
    直前runが無い・失敗ツールが無い・対象との交差が空の場合は終了コード0で成功終了する。

## 個別ツールを絞り込んで実行したい場合 {#single-tool}

特定のツール1件だけを実行したいときは`--commands=<tool>`オプションを使う。

```shell
pyfltr run --commands=textlint docs/
pyfltr run-for-agent --commands=mypy src/
```

サブコマンドは`run`または`run-for-agent`を利用する。
`pyfltr textlint docs/`のようにツール名をそのままサブコマンドへ書くことはできない
（誤入力を検知した場合は実行例付きのエラーメッセージが表示される）。

`run-for-agent`サブコマンドのJSONL出力に含まれる`command.retry_command`も同じ`--commands=<tool>`書式で生成される。
失敗ツールだけを再実行したい場合は、該当`command`レコードの`retry_command`をそのまま貼り付けて実行できる。

## pre-commitとの統合

pyfltrは`.pre-commit-hooks.yaml`を同梱していない。
pre-commitから呼び出したい場合は`.pre-commit-config.yaml`の`repo: local`でlocal hookとして登録する。
entryには`uvx pyfltr`を指定する（`uvx`でキャッシュされるため2回目以降は実用速度）。

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

dev依存に`pyfltr`を固定する運用では`entry: uv run --frozen pyfltr fast`に置き換えてもよい。

### 共通の注意点

- `pyfltr fast` はfixステージを内蔵する。pre-commit hookから`{command}-fix-args`定義済みlinter
 （`cargo-clippy` / `ruff-check` / `textlint`等）の自動修正が実行されるため、別hookを並べる必要は無い
- formatter（`ruff-format` / `prettier` / `cargo-fmt` / `dotnet-format`等）は通常実行で常時書き込みモードで動作するため、
  fixステージでは扱わない
- `pass-filenames = False`のツール（`cargo-*` / `dotnet-*` / `tsc`等）はcrate / solution全体を対象とするため、
  コミット時に未変更ファイルまで書き換わる可能性がある。
  cargo系・dotnet系は`serial_group`で自動直列化されるので、利用者が`--jobs=1`などを指定する必要は無い
