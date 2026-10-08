# 設定項目

`pyproject.toml`で設定する。
このページは設定項目のリファレンスで、最小例 → プリセット設定 → 言語カテゴリによる有効化の限定 → 設定項目一覧 →
グローバル設定 → ツール別除外・自動オプション・並列実行などの補助機能、の順に並ぶ。
導入手順は[はじめに](getting-started.md)を参照。

## 例

```toml
[tool.pyfltr]
preset = "latest"
pylint-args = ["--jobs=4"]
extend-exclude = ["foo", "bar.py"]
```

## プリセット設定 {#preset}

プリセットは各時点での推奨ツール構成をバージョン付きで示すスナップショット。
Python / JavaScript / TypeScript / Rust / .NET / ドキュメント系の推奨ツールを横断的に収録する。
`"latest"`または日付指定（`"20260926"` / `"20260726"` / `"20260413"` / `"20260411"` / `"20260330"`）を指定する。

```toml
[tool.pyfltr]
preset = "latest"
```

`preset = "latest"`はpyfltrの更新に伴って対象ツールの追加や既定値の変更が予告なく入ることがある。
破壊的変更を避ける場合は日付指定プリセットで固定すると、指定した日時点の構成をそのまま維持できる。

プリセットで`true`になっているツールも、次節の言語カテゴリキーが`true`の言語分だけが実際に実行される。
`preset = "latest"` + `{language} = true`だけで、その言語の推奨ツール一式が有効化される運用を意図している。

### preset "20260926"

`latest`が参照するプリセット。
`"20260726"`へ`pinact = true`を加えた構成。

### preset "20260726"

`"20260413"`から`pre-commit = true`を`prek = true`へ差し替えた構成。

### preset "20260413"

以下を設定する。

Python核（`python = true`で通過）

- `ruff-format = true`
- `ruff-check = true`
- `mypy = true`
- `pylint = true`
- `pyright = true`
- `pytest = true`
- `uv-sort = true`

JavaScript / TypeScript（`javascript = true`で通過）

- `eslint = true`
- `biome = true`
- `oxlint = true`
- `prettier = true`
- `tsc = true`
- `vitest = true`

Rust（`rust = true`で通過）

- `cargo-fmt = true`
- `cargo-clippy = true`
- `cargo-check = true`
- `cargo-test = true`
- `cargo-deny = true`

.NET（`dotnet = true`で通過）

- `dotnet-format = true`
- `dotnet-build = true`
- `dotnet-test = true`

ドキュメント系と統合系（言語カテゴリに属さず、言語カテゴリキーの影響を受けない）

- `textlint = true`
- `markdownlint = true`
- `actionlint = true`
- `typos = true`
- `pre-commit = true`

### preset "20260411"

`"20260413"`から`pre-commit = true`を除いた構成。
`pre-commit = true`を含むプリセットは`"20260413"`のみ。

### preset "20260330"

`"20260411"`から`actionlint = true` / `typos = true` / `uv-sort = true`を除いた構成。

## 言語カテゴリによる有効化の限定

各言語カテゴリに属するツールの既定値は無効（opt-in）。
プロジェクトで利用する言語カテゴリキーを`true`にすると、プリセットで推奨されたその言語のツールが有効化される。
カテゴリキーを`false`（既定）にすると、プリセットで`true`になっていても`false`に上書きされる。

`preset = "latest"` + `{language} = true`の組み合わせだけで、その言語の推奨ツール一式が有効化される。

個別のツール単位では`{command} = true`での有効化・`{command} = false`での無効化も可能で、
適用優先度は`preset < 言語カテゴリによる限定 < 個別設定`。

```toml
[tool.pyfltr]
preset = "latest"
python = true
```

各言語カテゴリキーと対象ツールは次の通り。

- `python`: ruff-format・ruff-check・mypy・pylint・pyright・ty・arid・pytest・uv-sort
- `javascript`: eslint・biome・oxlint・prettier・tsc・vitest（TypeScriptも同一カテゴリ）
- `rust`: cargo-fmt・cargo-clippy・cargo-check・cargo-test・cargo-deny
- `dotnet`: dotnet-format・dotnet-build・dotnet-test

カテゴリ同士は独立して作用する。
たとえば`python = true`を指定してもJavaScript系やRust系のツールは有効化されない。
Python系ツール一式は本体依存に同梱されているため、`uvx pyfltr`単発で利用できる。
JavaScript系・Rust系・.NET系は各言語のツールチェイン（Node.js・cargo・dotnet CLI）が前提となる。

対応するPython系ツールはruff-format / ruff-check / mypy / pylint / pyright / ty / arid / pytest / uv-sortの9種。
このうちtyのみpreset非収録のため、必要に応じて個別に`ty = true`を指定する（個別設定が最優先）。

```toml
[tool.pyfltr]
preset = "latest"
python = true
ty = true        # preset 非収録のため個別指定で追加
```

## 設定項目一覧 {#config-keys}

設定項目と既定値は`pyfltr config list`で確認できる。
未指定時は`pyproject.toml`に明示された値のみを挿入順で表示する。
全キー一覧（既定値のままのキーを含む）を確認するときは`--all`を付ける。

```sh
pyfltr config list --all
```

`--all`指定時は`DEFAULT_CONFIG`を起点にpyproject.toml値をマージし、キー昇順で出力する。
出力形式ごとに既定値か明示値かを区別する。

- text: 既定値の行末に`(default)`を付加する（明示値には何も付かない）
- json: `{"values": {key: {"value": ..., "default": bool}, ...}}`の二段構造で返す
- jsonl: 各行に`default: bool`フィールドを追加する

`{command}`系の項目およびツール固有の項目（`prettier-check-args`など）の詳細はツール別設定ページを参照。

- preset : プリセット設定（前述）
- python : Python系ツールの有効化（前述）
- javascript : JavaScript / TypeScript系ツールの有効化（前述）
- rust : Rust系ツールの有効化（前述）
- dotnet : .NET系ツールの有効化（前述）
- {command} : 各コマンドの有効/無効
- {command}-path : 実行するコマンド
- {command}-args : 追加のコマンドライン引数（lint/fix両モードで常に付与）
- {command}-lint-args : 非fixモードで付与する引数（既定はtextlintのみ`["--format", "compact"]`）
- {command}-fast : `fast`サブコマンドに含めるか否か（後述）
- pytest-fast-targets : fast実行時だけpytestの対象を置き換えるglob（既定: 空。[後述](#pytest-fast-targets)）
- pytest-always-targets : すべての実行でpytestの対象へ加えるglob（既定: 空。[後述](#pytest-always-targets)）
- {command}-fix-args : fix段で`{command}-args`の後に追加する引数
 （既定値はtextlint / markdownlint / ruff-check / eslint / biomeのみ定義）
- {command}-targets : 対象ファイルパターンの完全上書き
- {command}-extend-targets : 対象ファイルパターンへの追加
- {command}-exclude : ツール別の追加除外パターン（後述）
- {command}-pass-filenames : ファイル引数をコマンドに渡すか否か（既定: `true`）
- {command}-runner : ツール起動方式。
  カテゴリ委譲値（`"python-runner"` / `"js-runner"` / `"bin-runner"`）または直接指定値（9種）の対称12値を許容する。
  既定値はツールごとに異なる（[ツール別設定](configuration-tools.md#command-runner)を参照）
- python-runner : `{command}-runner = "python-runner"`の解決先（`"uv"` / `"uvx"` / `"direct"`、既定: `"uv"`）
- js-runner : `{command}-runner = "js-runner"`の解決先（`"pnpx"` / `"pnpm"` / `"npm"` / `"npx"` / `"yarn"` / `"direct"`、既定: `"pnpx"`）
- bin-runner : `{command}-runner = "bin-runner"`の解決先（`"mise"` / `"direct"`、既定: `"mise"`）
- {command}-version : bin-runner対応ツールのバージョン指定（既定: `"latest"`）
- pylint-pydantic : pylint実行時に`--load-plugins=pylint_pydantic`を自動追加するか（既定: `true`、後述）
- mypy-unused-awaitable : mypy実行時に`--enable-error-code=unused-awaitable`を自動追加するか（既定: `true`、後述）
- lychee-max-concurrency : lychee実行時の同時接続数（既定: `4`。`0`以下でlychee既定値へ委ねる、後述）
- jobs : linters/testersの最大並列数（既定: 8。CLIの`-j`オプションでも指定可能）。モノレポ検出時は同一ツールのサブプロジェクト実行を同時に開始する件数の算出にも使われる
- command-timeout : コマンド単体のタイムアウト秒数のグローバル既定値（既定: 600秒。0で無効化）
- {command}-timeout : per-toolのタイムアウト秒数。未指定時は`command-timeout`のグローバル値にフォールバックする。
  正の秒数を指定すると指定したコマンドのみその値で上書きし、`0`を指定すると同じコマンドのtimeoutのみ無効化する。
  内部的には負値を「未指定」のsentinelとして扱うため、誤って負値を設定した場合もグローバル値へフォールバックする
- retry-on-oom : LinuxのOOM killer起因でツールが強制終了された場合に自動リトライするか（既定: `true`。後述）
- retry-max-attempts : OOM検知時の最大リトライ回数（既定: `1`。`0`でリトライ無効。後述）
- exclude : 除外するファイル名/ディレクトリ名パターン（既定値あり。ロックファイル・minify済みファイル・source mapも含む）
- extend-exclude : 追加で除外するファイル名/ディレクトリ名パターン（既定は空）
- respect-gitignore : `.gitignore`に記載されたファイルを除外するか否か（既定: `true`）。
  gitのルートおよびネストした`.gitignore`、グローバルgitignore、`.git/info/exclude`を全て考慮する。`git`コマンドが必要
- pre-commit-auto-skip : `.pre-commit-config.yaml`からpyfltr関連hookを自動検出してSKIP環境変数に追加するか（既定: `true`）
- pre-commit-skip : SKIP環境変数に渡すhook IDの手動指定リスト（`pre-commit-auto-skip`と併用可能、既定: 空）
- prek-auto-skip : `.pre-commit-config.yaml`からpyfltr関連hookを自動検出してprekへ渡すSKIP環境変数に追加するか（既定: `true`）
- prek-skip : prekへ渡すSKIP環境変数のhook ID手動指定リスト（`prek-auto-skip`と併用可能、既定: 空）
- archive : 実行アーカイブの有効/無効（既定: `true`。`--no-archive`で実行単位に無効化）
- archive-max-runs : 保存する最大世代数（既定: 100。0以下で世代軸の自動削除を無効化）
- archive-max-size-mb : アーカイブ全体の合計サイズ上限（既定: 1024 MB。0以下でサイズ軸の自動削除を無効化）
- archive-max-age-days : 保存期間の上限（日数。既定: 30。0以下で期間軸の自動削除を無効化）
- cache : ファイルhashキャッシュの有効/無効（既定: `true`。`--no-cache`で実行単位に無効化）
- cache-max-age-hours : キャッシュエントリの保存期間（時間。既定: 12。0以下で期間軸の自動削除を無効化）
- replace-history-max-entries : `replace`サブコマンドの履歴の最大世代数（既定: 100。0以下で世代軸の自動削除を無効化）
- replace-history-max-size-bytes : replace履歴全体の合計サイズ上限（バイト単位。
  既定: `200 * 1024 * 1024`（約200 MiB）。0以下でサイズ軸の自動削除を無効化）
- replace-history-max-age-days : replace履歴の保存期間の上限（日数。既定: 30。0以下で期間軸の自動削除を無効化）
- jsonl-diagnostic-limit : 1ツールあたりのdiagnostic出力件数上限（既定: 0 = 無制限）
- jsonl-message-max-lines : `tool.message`の行数上限（既定: 30）
- jsonl-message-max-chars : `tool.message`の文字数上限（既定: 2000）
- textlint-protected-identifiers : textlint fixで破損させてはならない識別子のリスト。
  既定値は`[".NET", "Node.js", "Vue.js", "Next.js", "Nuxt.js"]`。
  詳細は[ツール別設定](configuration-tools.md#textlint-protected-identifiers)を参照
- subproject-exclude : モノレポ検出時に走査・サブプロジェクト集合から除外するディレクトリ名の追加リスト。
  既定値は空。`.venv`・`node_modules`・`target`・`build`・`dist`・`.git`は常に除外する
- subproject-use-gitignore : モノレポ検出で`.gitignore`を尊重するか否か（既定: `true`）。
  `.gitignore`の対象と判定された候補は検出集合から除外する
- subproject-uv-workspace : `[tool.uv.workspace] members`を読み取ってサブプロジェクトに含めるか否か（既定: `true`）
- {command}-subproject-aware : 指定したツールをサブプロジェクト単位で分割実行するか否か（per-tool）。
  既定値はビルトイン定義に従う。
  `mypy`・`pylint`・`pytest`・`textlint`等は`true`、`typos`・`shellcheck`・`shfmt`・`pre-commit`・`prek`等は`false`

`prettier-check-args` / `prettier-write-args` / `shfmt-check-args` / `shfmt-write-args`などの
2段階実行向け引数はツール別設定ページで詳しく扱う。

## OOM自動リトライ

LinuxのOOM killerによってツールプロセスが強制終了された場合、pyfltrは自動的にリトライする。

`retry-on-oom`を`true`（既定）にすると、ツールのリターンコードが`-9`または`137`のときをOOM起因とみなして
リトライを試みる。タイムアウト超過によるSIGKILLはOOM起因とはみなさず、リトライの対象外とする。

`retry-max-attempts`はOOM検知時の最大リトライ回数を指定する。既定値は`1`（最大1回リトライ）。
`0`を指定するとリトライを無効化する（`retry-on-oom: false`と同じ効果）。

JSONL出力（`--output-format=jsonl`）では`command`レコードの`retry_count`フィールドにリトライ回数が記録される。
`retry_count`が`0`のときはフィールド自体が省略される。

Windows環境ではOOM killerが存在せずreturncodeが`-9`または`137`にならないため、本機能のリトライは実行されない。

## グローバル設定

プロジェクトをまたいで共通にしたい設定（archiveの保持期間やキャッシュ保存期間など）は、
ユーザーレベルのグローバル設定ファイルに書くことでマシン単位で集約できる。

### グローバル設定ファイルのパス

OS別のパスは次の通り（`platformdirs.user_config_dir("pyfltr")`が解決する場所）。

- Linux: `~/.config/pyfltr/config.toml`
- macOS: `~/Library/Application Support/pyfltr/config.toml`
- Windows: `%LOCALAPPDATA%\pyfltr\config.toml`

環境変数`PYFLTR_GLOBAL_CONFIG`を設定するとそのパスを優先する
（テスト容易性の確保やユーザーによる強制上書きを目的とした差し替え用。`PYFLTR_CACHE_DIR`と命名対称）。

### 書式

`pyproject.toml`と同じ形式で、`[tool.pyfltr]`セクション配下にキーを列挙する。

```toml
[tool.pyfltr]
archive-max-age-days = 30
archive-max-size-mb = 2048
cache-max-age-hours = 24
```

全項目をグローバル設定ファイルに書くことができる。
ただし、archive系とcache系の計6キーのみ特殊仕様で、グローバル設定が優先される。

- archive系（4キー）: `archive` / `archive-max-runs` / `archive-max-size-mb` / `archive-max-age-days`
- cache系（2キー）: `cache` / `cache-max-age-hours`

これらをproject側の`pyproject.toml`に書いた場合は警告が出る。
それ以外のキーはproject側が優先されるため、グローバル設定は未設定時のフォールバックとして機能する。

### 設定の適用順

1. 既定値を生成する
2. グローバル設定とproject設定（`pyproject.toml`）を読み込み、1つの入力にマージする
   - archive/cache系はマージ時にグローバル側を優先する（project側に同じキーがあっても上書きされる）
   - それ以外のキーは後勝ち（project側が優先）
3. マージ結果にプリセット（`preset`）を反映する
4. 言語カテゴリによる限定（`python` / `javascript` / `rust` / `dotnet`）を適用する

適用優先度は`preset < 言語カテゴリによる限定 < 個別設定`。
`pyproject.toml`が存在しないディレクトリでもグローバル設定は反映される。

### 設定操作

`pyfltr config`サブコマンドを使うと、project側の`pyproject.toml`とglobal側の`config.toml`の
両方をCLIから直接操作できる。
`--global`の有無で対象ファイルを切り替える。
詳細は[CLIコマンド](usage.md#config)を参照。

### 不正な設定値の取り扱い

設定ファイル（`pyproject.toml`およびグローバル設定）で未知のキー・型不一致・許容外の値・削除済みコマンド向けのキーを
検出した場合、該当キーを無視して既定値で処理を続行する。検出内容は警告として記録する。

本挙動は複数バージョンのpyfltrが同じ設定ファイルを参照する状況で、
新しいバージョンが追加したキーを旧バージョンが読み込んだ際の停止を回避するために採用する。
コマンドラインからの設定上書き（`--config=key=value`等）は対象外で、従来通りエラーで停止する。

## 対象ファイルパターンのカスタマイズ

各コマンドが処理する対象ファイルパターンを変更できる。

`{command}-targets`でパターンを完全に上書きする。

```toml
[tool.pyfltr]
# shfmtの対象を *.bash のみに変更（既定の *.sh は対象外になる）
shfmt-targets = ["*.bash"]
```

`{command}-extend-targets`で既存パターンに追加する。

```toml
[tool.pyfltr]
# shfmtの対象に *.sh.tmpl と dot_bashrc を追加（既定の *.sh も維持）
shfmt-extend-targets = ["*.sh.tmpl", "dot_bashrc"]
shellcheck-extend-targets = ["*.sh.tmpl", "dot_bashrc"]
```

両方を指定した場合、`targets`で上書きした後に`extend-targets`で追加する。

## 引数の追加

各コマンドの引数を変更したい場合、`{command}-args`で既定値を完全に上書きするか、
`{command}-extend-args`で既定値を保ったまま末尾へ引数を追加する。

```toml
[tool.pyfltr]
# 既定値を完全に上書きする（pyfltr既定の必須引数も含めて全て指定する必要がある）
lychee-args = ["--format", "json", "--no-progress", "--exclude=github\\.com/owner/repo/actions"]

# 既定値を保ったまま末尾へ引数を追加する
lychee-extend-args = ["--exclude=github\\.com/owner/repo/actions"]
```

`{command}-extend-args`の対象は共通引数`{command}-args`のみ。
`{command}-lint-args`・`{command}-fix-args`・`{command}-check-args`・`{command}-write-args`等の
モード専用引数には対応しない。モード切替時の引数は既定値を尊重する設計のため。

## pass-filenames設定 {#pass-filenames}

`{command}-pass-filenames = false`を設定すると、コマンド実行時にファイル引数を渡さない。
プロジェクト全体を一括チェックするツール（`tsc`など）で使用する。

ビルトインでは、プロジェクト全体を単位として動作する以下のツールが`pass-filenames = false`に設定されている。

- `tsc`
- `cargo-fmt` / `cargo-clippy` / `cargo-check` / `cargo-test` / `cargo-deny`
- `dotnet-format` / `dotnet-build` / `dotnet-test`

pre-commit・prekの呼出しとhookのファイル選定は[CLIコマンドの統合説明](usage.md#precommit-integration)を参照。

カスタムコマンドでも同様に設定可能。

```toml
[tool.pyfltr]
tsc = true
# tsc は既定で pass-filenames = false のため明示不要

[tool.pyfltr.custom-commands.commitlint]
type = "linter"
path = "commitlint"
args = ["--from=HEAD~1"]
pass-filenames = false
```

## severityによる失敗の警告化 {#severity}

`{command}-severity`を`"warning"`に設定すると、そのツールの失敗をJSONL `command.status="warning"` で記録し、
パイプライン全体のexit codeに影響させない扱いに切り替えられる。
口語表現検出など「警告で十分」な用途で、エージェントを止めずに通知だけしたい場合に使う。
カスタムコマンド・ビルトイン共通で利用できる。

```toml
[tool.pyfltr]
colloquial-check = true
colloquial-check-severity = "warning"
```

許容値は`"error"`（既定）と`"warning"`の2値。
`"warning"`設定下では`commands_summary.needs_action.warning`に集計し、
`summary.guidance`のfailure系文言は出力しない。

ツール起動自体に失敗するケース（`resolution_failed` / `timeout_exceeded`）は`severity`の影響を受けない。
ツール起動側の異常で警告扱いに馴染まないため、`failed`/`resolution_failed`のままとなる。

## hintsによるLLM向け補足 {#hints}

`{command}-hints`に文字列配列を指定すると、JSONL `command.hints` に `user.<n>` 連番キーで埋め込む。
LLMエージェントへ修正方針や参考文献を渡したいときに使う。
配列要素は英語推奨（`command.hints` / `summary.guidance` と同じくLLM入力前提のため）。

```toml
[tool.pyfltr]
colloquial-check = true
colloquial-check-hints = [
    "Replace colloquial Japanese expressions with formal written-language equivalents.",
]
```

指摘1件以上のときに限り出力する（指摘0件の実行で固定的なhintを残してLLM入力のトークンを浪費しないため）。
ビルトインコマンドにも同名キーで指定可能で、利用者ごとの運用ノウハウを永続化する用途に利用できる。

## `~`展開

対応キーは以下。
これらに`~`を含めると、subprocess引数組み立て直前に展開する。
利用者ホーム配下に置いた個人ツールスクリプトをそのまま参照したい場合に使う。

- `{command}-path`
- `{command}-args` / `{command}-extend-args` /
  `{command}-lint-args` / `{command}-fix-args`
- `{command}-check-args` / `{command}-write-args`
- `ruff-format-check-args`

```toml
[tool.pyfltr.custom-commands.my-tool]
type = "linter"
path = "uv"
args = ["run", "--script", "~/dotfiles/scripts/my_tool.py"]
```

展開規則は2点。
要素先頭が`~`または`~user`の場合は`os.path.expanduser`で展開する。
要素内最初の`=`直後の`~`/`~user`も同様に展開する。
そのため`"--config=~/cfg.toml"`形式と`["--config", "~/cfg.toml"]`の分割形式のどちらでも同じく展開される。

`config-files` / `targets` / `{command}-extend-targets`等のglobパターンには展開を適用しない
（glob内チルダの意図しない展開を防ぐため）。

## ツール別除外設定

`{command}-exclude`を設定すると、特定ツールにのみ適用する追加の除外パターンを指定できる。
全体の`exclude`/`extend-exclude`による除外はこれとは独立して事前に適用される。

```toml
[tool.pyfltr]
# mypy だけ vendor/ と gen_*.py を除外する
mypy-exclude = ["vendor", "gen_*.py"]
```

パターンの書式は`extend-exclude`と同じflake8風のglobパターンで、ディレクトリ指定はその配下も除外される。
`--no-exclude`を指定した場合、全体の`exclude`/`extend-exclude`と合わせてツール別除外も無効化される。

`pass-filenames = false`のツール（tsc・cargo-\*・dotnet-\*など）はファイル名をコマンドに渡さないため、
`{command}-exclude`を設定しても効果がない。

## 自動オプション

各ツールの望ましいオプションを自動的にコマンドラインに追加する。
`{command}-args`とは独立して動作する。

| 設定 | 既定 | 自動追加される引数 |
| --- | --- | --- |
| `pylint-pydantic` | `true` | `--load-plugins=pylint_pydantic` |
| `mypy-unused-awaitable` | `true` | `--enable-error-code=unused-awaitable` |
| `lychee-max-concurrency` | `4` | `--max-concurrency=4` |

自動引数は`{command}-args`やCLI引数と重複しないよう排除される。
真偽値の設定は不要な場合に`false`を、数値の設定はツール側の既定値へ委ねる場合に`0`以下を指定する。

```toml
[tool.pyfltr]
pylint-pydantic = false
mypy-unused-awaitable = false
lychee-max-concurrency = 0
```

## 並列実行

linters/testersは`jobs`で指定した並列数で実行される（既定: 8）。

```toml
[tool.pyfltr]
jobs = 8
```

CLIオプション`-j`でも指定でき、`pyproject.toml`より優先される。

実行順は`fast`フラグに基づいて最適化され、`fast = false`のツール（mypy、pylint、pytest等）が先に開始される。

既定値をホストの論理CPU数へ合わせていないのは、各ツールが実行中に常時CPUを占有するわけではなく、
プロセスの起動やファイルの読み書きで待つ時間があるためである。

モノレポ構成では、同一ツールをサブプロジェクトごとに実行する分も`jobs`から並列化する。
`jobs`をそのツール自身のワーカー数の推定値で割った件数を同時に開始する。
推定には`pytest-args`・`pylint-args`などの`-n`・`--jobs`指定と、各サブプロジェクトの`pyproject.toml`の
`[tool.pytest.ini_options]`の`addopts`・`[tool.pylint]`の`jobs`を用いる。
`jobs = 8`のもとで`pytest`へ`-n 4`を指定している場合は2件ずつ実行され、
ツール自身のワーカー数との積が`jobs`を超えない。

`jobs`がツール自身のワーカー数の推定値以下になる場合は、サブプロジェクトを1件ずつ順に実行する。
並列化しても報告順はサブプロジェクトの相対パスの昇順で安定する。

## fastエイリアス

`fast`サブコマンドで実行されるコマンドは、各コマンドの`{command}-fast`設定で制御される。

```toml
[tool.pyfltr]
# mypyをfastに追加
mypy-fast = true
# textlintをfastから除外
textlint-fast = false
```

カスタムコマンドも`fast = true`でfastエイリアスに追加できる。

### fast実行時のpytest対象 {#pytest-fast-targets}

`pytest-fast-targets`へglob（文字列または文字列の配列）を指定すると、fast実行時のpytestは一致するテストファイルだけを実行する。
変更ファイルに依らず毎回確かめたいテスト（リポジトリ全体の不変条件を確かめるテストなど）を、全テストを収集せずにpre-commitから実行する用途を想定する。

```toml
[tool.pyfltr]
pytest = true
pytest-fast-targets = ["*_invariant_test.py"]
```

- 値が空でなければ、`pytest-fast = false`でもpytestはfastエイリアスに含まれる。`pytest = false`の場合は実行しない
- fast実行は`pyfltr fast`と、`--commands`へ`fast`を含む実行（`pyfltr run --commands=fast`など）を指す。`pyfltr fast --commands=pytest`も含む
- 対象は位置引数と`--changed-since`に依らず、プロジェクト全域から選ぶ。Markdownだけを変更したコミットでも指定したテストが動く
- globの一致は`pytest-targets`と同じ規則で判定し、`pytest-targets`の代わりに使う。`exclude`・`extend-exclude`・`.gitignore`・`pytest-exclude`による除外はそのまま適用する
- 一致するファイルが無い場合はpytestを起動せず、対象ファイル0件として扱う（全テストの収集へは戻らない）
- `pytest-args`などの引数は通常どおり渡す。マーカー式は追加しない
- fast以外の実行（`pyfltr run`・`pyfltr ci`、`--commands=pytest`など）の対象は変わらない。値が空（既定）の場合もfastの対象と参加判定は従来どおりとなる
- モノレポでは各サブプロジェクトの設定に従い、そのサブプロジェクトのファイルだけをそのディレクトリで実行する。起点に指定が無くても、指定を持つサブプロジェクトがあればfastでpytestを実行する

### 常に加えるpytest対象 {#pytest-always-targets}

`pytest-always-targets`へglob（文字列または文字列の配列）を指定すると、pytestを実行するすべての実行で、一致するテストファイルを通常の対象へ加える。
変更したファイルだけを渡した実行でも、別のファイルに定義した不変条件のテスト（リポジトリ全体の状態を確かめるテストなど）を同じ実行で動かす用途を想定する。

```toml
[tool.pyfltr]
pytest = true
pytest-always-targets = ["*_invariant_test.py"]
```

- 対象は「位置引数と`--changed-since`で決まる通常の対象」と「プロジェクト全域から指定globに一致したファイル」の和集合になる。同じファイルは1回だけ渡す
- `pyfltr run`・`run-for-agent`・`ci`・MCPの`run`とfast実行に適用する。fastでは`pytest-fast-targets`で置き換えた対象へ加える
- `pytest-fast-targets`は置き換え、`pytest-always-targets`は追加という違いがある。fastへの参加は`pytest-fast`と`pytest-fast-targets`が決め、`pytest-always-targets`だけではpytestをfastへ加えない
- `pytest = false`の場合と、`--commands`でpytestを選ばない実行では実行しない
- `exclude`・`extend-exclude`・`.gitignore`・`pytest-exclude`による除外と、それらを無効化する`--no-exclude`・`--no-gitignore`はそのまま適用する
- `--only-failed`では、追加したテストも候補に含めたうえで前回失敗したファイルだけを実行する
- モノレポでは各サブプロジェクトの設定に従い、そのサブプロジェクトのファイルだけをそのディレクトリで実行する。変更したファイルが別のサブプロジェクトにだけある場合も、指定を持つサブプロジェクトのテストを実行する

## 出力順序

非TUIモード（`--no-ui`、`--ci`、または非対話端末）では、既定の動作として全コマンドの完了後に
成功コマンド詳細 → 失敗コマンド詳細 → `summary`の順でまとめて出力する。
`pyfltr ... | tail -N`のようにパイプで末尾だけ切り出してもsummaryと失敗情報が末尾に残るため、
Claude Codeなど末尾だけを読み取るツールでも実行結果を把握できる。

従来の「各コマンドの完了時に即座に詳細ログを出力する」挙動を使いたい場合は`--stream`を指定する。

---

個別のツール設定（2段階実行、直接実行 / js-runner / bin-runnerのカテゴリ別設定、
`mise-auto-trust`によるmise未信頼の自動対応等）の詳細は[ツール別設定](configuration-tools.md)、
カスタムコマンドの仕様と事例は[プロジェクト固有チェックの追加](custom-commands.md)を参照。
