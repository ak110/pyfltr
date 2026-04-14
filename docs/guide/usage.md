# CLIコマンド

## サブコマンド

pyfltrはサブコマンドで動作モードを指定する。

```shell
pyfltr <subcommand> [files and/or directories ...]
```

### サブコマンド: ci（既定）

```shell
pyfltr ci [files and/or directories ...]
pyfltr [files and/or directories ...]  # ciは省略可能
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

### サブコマンド: fast

```shell
pyfltr fast [files and/or directories ...]
```

pre-commitフックなどで実行しても作業に支障が出にくい高速なコマンドだけを実行する軽量チェック。
mypy / pylint / pytestなど起動やファイルあたりの処理に時間がかかるコマンドは除外される。
Formattersによるファイル変更があっても終了コードは0になる。

既定で含まれるコマンドは以下。

- Formatters: `pyupgrade` `autoflake` `isort` `black` `ruff-format` `prettier` `uv-sort` `shfmt` `cargo-fmt` `dotnet-format`
- Linters: `ec` `shellcheck` `typos` `actionlint` `ruff-check` `pflake8` `ty` `markdownlint` `textlint` `biome` `oxlint` `cargo-clippy`

含まれるコマンドは各コマンドの`{command}-fast`設定で制御できる（[設定](configuration.md)を参照）。

### subcommand: generate-config

```shell
pyfltr generate-config
```

設定ファイルの雛形を標準出力に書き出す。`[tool.pyfltr]`セクションに貼り付けて利用する。
このサブコマンドは他のオプションやターゲット指定を受け付けず、設定出力だけを行う。

### `[files and/or directories ...]`

対象を指定しなかった場合は、カレントディレクトリ(`.`)を指定した場合と同じ扱いとなる。

指定したファイルやディレクトリの配下のうち、各コマンドのtargetsパターンに一致するファイルのみ処理される。
一例を以下に示す。

- Python系ツール: `*.py`
- markdownlint / textlint: `*.md`
- pytest: `*_test.py`

### `fast` / `run` / `ci`の動作の違いと自動修正（fixステージ）

3サブコマンドの主な違いを以下に示す（軽い順）。

| 項目 | `fast` | `run` | `ci` |
| --- | --- | --- | --- |
| 対象コマンド | `{command}-fast = true`のツールのみ | 有効な全ツール | 有効な全ツール |
| fixステージ（自動修正） | 有効 | 有効 | 無効 |
| Formatterによる変更時の終了コード | `0`（成功扱い） | `0`（成功扱い） | `1`（失敗扱い） |
| Linters / Testersのエラー時の終了コード | `1` | `1` | `1` |
| 主な用途 | pre-commitフックなどの軽量チェック | ローカルでの全チェック実行 | CI・コミット前チェック |

`fast` / `run` サブコマンドは、formatter段の前にfixステージを内蔵する。

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
- `--no-exclude`: exclude/extend-excludeパターンによるファイル除外を無効化する
- `--no-gitignore`: `.gitignore`によるファイル除外を無効化する
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

### jsonl形式の使い方

```shell
pyfltr run --output-format=jsonl
```

`--output-format=jsonl`かつ`--output-file`未指定時、stdoutにはJSONLのみを書き、既存の`text`ログ（進捗・詳細・summary）は完全に抑止される。TUIや`--stream`、`--ui`も暗黙に無効化される。

`--output-file=path`を指定するとJSONLはファイルへ書き出され、stdoutには従来どおりの`text`出力が並行して出る（ローカル実行時も開発者が進捗を追える）。

### jsonlスキーマ

出力は以下3種別のレコードからなる。`kind`フィールドでレコード種別を判別する。

- `diagnostic`: 個々の診断（`error_parser`対応ツールの抽出結果）
- `tool`: 1ツール1レコードの実行メタ情報
- `summary`: 最終1行、全体集計

```json
{"kind":"diagnostic","tool":"mypy","file":"src/a.py","line":42,"col":5,"msg":"Incompatible return value type"}
{"kind":"tool","tool":"mypy","type":"linter","status":"failed","files":12,"elapsed":0.8,"diagnostics":1,"rc":1}
{"kind":"tool","tool":"black","type":"formatter","status":"formatted","files":12,"elapsed":0.3,"diagnostics":0,"rc":1}
{"kind":"summary","total":2,"succeeded":0,"formatted":1,"failed":1,"skipped":0,"diagnostics":1,"exit":1}
```

出力順は`diagnostic`（file/line/col/command順）→`tool`（`pyproject.toml`の定義順）→`summary`（1行）で固定。`tail -1`で`summary`だけ取り出せる。

`diagnostic`レコードの`col`は抽出できた場合のみ含まれる。`tool`レコードの`rc`は`returncode is not None`のときのみ含まれる（`skipped`では省略）。

`tool`レコードは`status == "failed"`かつ`diagnostics == 0`のときに限り、`message`フィールドに`CommandResult.output`の末尾をトリム（30行・2000文字の短い方）した内容を含める。実行ファイル未検出など、`error_parser`でパースできない失敗理由を捕捉するため。

`summary`レコードの`diagnostics`キーは全ツール合算の診断数で、個別の`tool`レコードの`diagnostics`と集計名を統一している。

### LLM連携の例

`Claude Code`などで`pyfltr`の結果を参照する場合、jsonl形式が扱いやすい。

ファイル末尾の`summary`行を読めば全体像を把握でき、必要に応じて`diagnostic`行を参照することでトークン消費を抑えられる。

LLMエージェントがpyfltrを活用する基本的な流れ:

1. 全体実行でsummaryを確認する

    ```shell
    pyfltr run --output-format=jsonl
    ```

    末尾のsummary行（`"kind":"summary"`）で`failed`の有無と`diagnostics`数を確認し、問題がなければ完了する。

2. 問題があるツール/ファイルだけ個別に再実行する

    ```shell
    pyfltr run --commands=mypy --output-format=jsonl path/to/file.py
    ```

    `--commands`で特定ツールに絞ることで出力量を抑えつつ、`diagnostic`行から修正対象のファイル・行番号・メッセージを取得する。
    詳細が必要な場合に限り`--output-format=text`で再実行するなど、段階的に情報を掘り下げることも可能。

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
- formatter（`black` / `prettier` / `cargo-fmt` / `dotnet-format`等）は通常実行で常時書き込みモードで動作するため、fixステージでは扱わない
- `pass-filenames = False`のツール（`cargo-*` / `dotnet-*` / `tsc`等）はcrate / solution全体を対象とするため、コミット時に未変更ファイルまで書き換わる可能性がある。cargo系・dotnet系は`serial_group`で自動直列化されるので、利用者が`--jobs=1`などを指定する必要は無い
