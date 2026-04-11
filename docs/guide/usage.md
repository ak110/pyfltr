# 使い方

## サブコマンド

pyfltrはサブコマンドで動作モードを指定する。

```shell
pyfltr <subcommand> [files and/or directories ...]
```

対象を指定しなかった場合は、カレントディレクトリを指定した場合と同じ扱いとなる。

指定したファイルやディレクトリの配下のうち、各コマンドのtargetsパターンに一致するファイルのみ処理される。

- Python系ツール: `*.py`
- markdownlint / textlint: `*.md`
- pytest: `*_test.py`

### ci（既定）

```shell
pyfltr ci [files and/or directories ...]
pyfltr [files and/or directories ...]  # ciは省略可能
```

全チェック実行。CI環境やコミット前の検証に適する。

終了コード:

- 0: Formattersによるファイル変更が無く、かつLinters/Testersでのエラーも無い場合
- 1: 上記以外の場合

### run

```shell
pyfltr run [files and/or directories ...]
```

全チェック実行。Formattersによるファイル変更があってもLinters/Testersでのエラー無しなら終了コードは0になる。ローカルでの全チェック実行に適する。

### fast

```shell
pyfltr fast [files and/or directories ...]
```

mypy / pylint / pytestなど重いコマンドを除外した軽量チェック。Formattersによるファイル変更があっても終了コードは0になる。pre-commitフックなど速度を優先する場面に適する。

既定で含まれるコマンドは以下。

- Formatters: `pyupgrade` `autoflake` `isort` `black` `ruff-format` `prettier` `uv-sort` `shfmt` `cargo-fmt` `dotnet-format`
- Linters: `ec` `shellcheck` `typos` `actionlint` `ruff-check` `pflake8` `ty` `markdownlint` `textlint` `biome` `oxlint` `cargo-clippy`

含まれるコマンドは各コマンドの`{command}-fast`設定で制御できる（[設定](configuration.md)を参照）。

### fix

```shell
pyfltr fix [files and/or directories ...]
```

修正モード。linterの中でもautofix機能を持つtextlint / markdownlint / ruff-checkなどに、内部で `--fix` 相当の引数を追加して実行する。手動実行専用。

fixモードの対象は次の和集合となる。

- 有効化されたformatter全て（通常実行そのものがファイルを修正する）
- 有効化されており、かつ `{command}-fix-args` が定義されたlinter（ビルトインではtextlint / markdownlint / ruff-check / eslint / biomeが既定で対応）

fixモードの挙動。

- 全対象コマンドを順次実行する（同一ファイルへの書き込み競合を避けるため並列化しない）
- `--shuffle` は無効化される
- 対象が0件の場合はエラー終了する
- `--commands` と併用可能。併用時は展開後の結果に対して上記フィルタを適用する
- linterのfix実行後、ファイルmtimeの変化があれば `formatted`、変化が無ければ `succeeded`、終了コードが0以外なら `failed` となる
    - 特にruff-checkは未修正の違反が残ると終了コード1を返すため、`failed` 扱いとなる。通常モードの `ruff-check` で残存違反を別途確認すること

カスタムコマンドでも `pyproject.toml` の `[tool.pyfltr.custom-commands.<name>]` に `fix-args = [...]` を定義すればfixモードの対象にできる。

## 特定のツールのみ実行

```shell
pyfltr ci \
  --commands=pyupgrade,autoflake,isort,black,ruff-format,\
ruff-check,pflake8,mypy,pylint,pyright,ty,markdownlint,textlint,pytest \
  [files and/or directories ...]
```

カンマ区切りで実行するツールだけ指定する。全サブコマンドで使用可能。

以下のエイリアスも使用可能。(例: `--commands=format`)

- `format`: `pyupgrade` `autoflake` `isort` `black` `ruff-format` `prettier` `uv-sort` `shfmt` `cargo-fmt` `dotnet-format`
- `lint`:
    - Python系: `ruff-check` `pflake8` `mypy` `pylint` `pyright` `ty`
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

- `--no-ui`: UIを無効化し、出力を直接ターミナルに表示（エラー一覧の後にサマリーを表示）
- `--ci`: CI環境向け（`--no-shuffle --no-ui` 相当）
- `-j N` / `--jobs N`: linters/testersの最大並列数を指定（既定: 4、`pyproject.toml`でも設定可能）

その他のオプションは `pyfltr --help` を参照。

## 出力形式

`--output-format` で出力形式を切り替えられる。既定は `text`（従来の人間向け出力）。

| 値 | 用途 | 動作 |
| --- | --- | --- |
| `text` | 既定。人間向け・従来互換 | logging経由で進捗・詳細・summary をまとめて表示 |
| `jsonl` | LLMエージェント向け | JSON Lines形式で 1行 1診断＋末尾 summary行 |

`pyproject.toml`の`[tool.pyfltr]`にも`output-format = "jsonl"`として既定値を指定できる。CLIの`--output-format`で上書き可能。

### jsonl形式の使い方

```shell
pyfltr ci --output-format=jsonl --exit-zero-even-if-formatted | tee out.jsonl
```

`--output-format=jsonl`かつ`--output-file`未指定時、stdoutにはJSONLのみを書き、既存の`text`ログ（進捗・詳細・summary）は完全に抑止される。TUIや`--stream`、`--ui`もsilentlyに無効化される。

`--output-file=path`を指定するとJSONLはファイルへ書き出され、stdoutには従来どおりの`text`出力が並行して出る（ローカル実行時も開発者が進捗を追える）。

終了コードは従来と同じで、`--exit-zero-even-if-formatted`と組み合わせるとformatterによる修正だけで`exit 1`にならないようにできる。

### jsonlスキーマ

出力は以下3種別のレコードからなる。

- `diag`: 個々の診断（`error_parser`対応ツールの抽出結果）
- `tool`: 1ツール1レコードの実行メタ情報
- `summary`: 最終行1レコード、全体集計

```json
{"kind":"diag","tool":"mypy","file":"src/a.py","line":42,"col":5,"msg":"Incompatible return value type"}
{"kind":"tool","tool":"mypy","type":"linter","status":"failed","files":12,"elapsed":0.8,"diags":1,"rc":1}
{"kind":"tool","tool":"black","type":"formatter","status":"formatted","files":12,"elapsed":0.3,"diags":0,"rc":1}
{"kind":"summary","total":2,"succeeded":0,"formatted":1,"failed":1,"skipped":0,"diags":1,"exit":1}
```

出力順は`diag`（file/line/col/command順）→`tool`（`pyproject.toml`の定義順）→`summary`（1行）で固定。`tail -1`で`summary`だけ取れる。

`tool`レコードは`status == "failed"`かつ`diags == 0`のときに限り、`message`フィールドに`CommandResult.output`の末尾をトリム（30行・2000文字の短い方）した内容を含める。実行ファイル未検出など、`error_parser`でパースできない失敗理由を捕捉するため。

`diag`レコードの`col`は抽出できた場合のみ含まれる。`tool`レコードの`rc`は`returncode is not None`のときのみ（`skipped`では省略）。

### LLM連携の例

`Claude Code`などで`pyfltr`の結果を参照する場合、以下の形式が扱いやすい。

```shell
pyfltr ci --output-format=jsonl --exit-zero-even-if-formatted --output-file=.claude/lint.jsonl
```

ファイル末尾の`summary`行を読めば全体像が掴めて、必要に応じて`diag`行を参照することでトークン消費を抑えられる。

## pre-commit frameworkとの統合

pyfltrは`.pre-commit-hooks.yaml`を同梱していない。pre-commit frameworkから呼び出したい場合は、`.pre-commit-config.yaml`の`repo: local`でlocal hookとして`uv run pyfltr`または`uvx pyfltr`を呼び出す。pyfltrの実行方式をプロジェクトのuv環境と揃えられるため、依存管理・バージョン固定の観点で一元化できる。

### Pythonプロジェクト（pyfltrを`uv.lock`に含める構成）

pyfltr自身のリポジトリを含むPython系プロジェクトで採用している構成。`uv run --frozen pyfltr fast`を`uv.lock`ごとバージョン固定し、pre-commit時にキャッシュ済み`.venv`を再利用する。

```yaml
repos:
  - repo: local
    hooks:
      - id: pyfltr-fast
        name: pyfltr fast
        language: system
        entry: uv run --frozen pyfltr fast
        pass_filenames: true
        require_serial: true
        types: [file]
```

### 非Pythonプロジェクト（`uvx`で都度取得する構成）

Rust / .NETなどpyfltrを`uv.lock`に含めないプロジェクトでは`uvx pyfltr fast`を直接呼び出す。`mise.toml`で`uv`をツールとして定義しておけば、チームメンバー間で環境差異が出にくい。

```yaml
repos:
  - repo: local
    hooks:
      - id: pyfltr-fast
        name: pyfltr fast
        language: system
        entry: uvx pyfltr fast
        pass_filenames: true
        require_serial: true
        types: [file]
```

### 共通の注意点

- pre-commit hookで自動修正まで走らせたい場合は`entry`末尾に`--fix`を追加するか、local hookを`pyfltr-fast-fix`のように2つ目として並べる
- pyfltrの`--fix`は`{command}-fix-args`が定義されたlinter（`cargo-clippy` / `ruff-check` / `textlint`等）にのみ影響する。formatter（`black` / `prettier` / `cargo-fmt` / `dotnet-format`等）は`--fix`指定の有無に関わらず常時書き込みモードで動作する
- `pass-filenames = False`のツール（`cargo-*` / `dotnet-*` / `tsc`等）はcrate / solution全体を対象とするため、コミット時に未変更ファイルまで書き換わる可能性がある。cargo系・dotnet系は`serial_group`で自動直列化されるので、利用者が`--jobs=1`などを指定する必要は無い
