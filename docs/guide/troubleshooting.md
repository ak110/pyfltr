# トラブルシューティング

pyfltr運用中に発生しがちな事象と対処法を症状別にまとめる。
導入手順は[はじめに](getting-started.md)を参照。

## MCP起動時のstdout占有事故

`pyfltr mcp`起動後に他のコマンド出力やlogが端末に表示されず、
コーディングエージェントに返る結果が文字化けするまたは解析エラーになる場合がある。

`pyfltr mcp`を起動するとstdin/stdoutがJSON-RPCフレームに専有される。
同一プロセスや同一パイプライン内で他のテキスト出力が混入すると、
JSON-RPCパーサーがフレームを正しく解析できない。

回避策。

- `pyfltr mcp`は単独プロセスとして起動し、他のコマンドとパイプで繋がない
- コーディングエージェントのMCP設定では`command`に`uvx`を指定し、
  ラッパースクリプトを経由する場合はstdoutに余分な出力が混入しないことを確認する
- デバッグ目的でlogを確認したい場合はstderrにリダイレクトする
 （pyfltrはtextout・system logを常にstderrに出力するため、stderrは参照できる）

## MCPクライアントでツール認証に失敗する

MCPクライアントから`pyfltr mcp`を登録しても`run_for_agent`等のツール呼び出しでエラーになる場合がある。

確認手順。

- pyfltrを最新版へ更新する。古いMCPプロトコルバージョンでは互換性問題が発生する場合がある
- `command`には、PATHで解決できる`uvx`か、`uv tool install`で事前導入したpyfltr実行ファイルの絶対パスを使う
- クライアント側のMCPサーバーログ（Claude Codeであれば設定UIから確認可能）でJSON-RPCエラーの詳細を確認する
- stdoutに非JSON出力が混じっていないかをstderrへリダイレクトして確認する
 （`uvx pyfltr mcp 2>/tmp/pyfltr-mcp.err`のように手動起動して観察できる）

## MCPクライアントの起動handshakeが接続断で失敗する

MCPクライアントへ`pyfltr mcp`を登録した際、次のようなエラーが断続的に出る場合がある。

```text
MCP client for `pyfltr` failed to start: MCP startup failed:
handshaking with MCP server failed: connection closed: initialize response
```

`connection closed`は、MCPサーバーのプロセスが初期化応答を返す前に終了したことを示す。
原因はpyfltrの外側にある場合と内側にある場合の双方があるため、次の順で切り分ける。

### 切り分け手順

まず標準出力の汚染を除外する。`pyfltr mcp`の標準出力をJSON-RPC以外の内容が共有していると、
サーバーが起動していてもクライアントはフレームを解釈できない。
確認方法と対処は「MCP起動時のstdout占有事故」を参照する。

次に、クライアントへ登録しているコマンドをそのまま端末で起動し、終了コードと標準エラーを確かめる。

```sh
printf '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"probe","version":"1"}}}\n' \
  | uvx --from "pyfltr>=3.16" pyfltr mcp
echo "exit=$?"
```

- 標準出力へJSON-RPC応答が1行出て終了コードが0であれば、pyfltr側は正常に応答している。
  クライアント側の設定（起動待ち時間、作業ディレクトリ、環境変数）を確認する
- 標準出力が空のまま非0で終わり、標準エラーへ`uvx`のパッケージ解決の失敗
  （`Request failed after 3 retries`など）が出る場合は、pyfltrではなく起動コマンドが失敗している。
  原因1に当たる
- 応答は返るが待ち時間が長い場合は、起動処理のいずれかが遅い。
  この時間には依存の再構築だけでなく、Pythonプロセスの起動とpyfltr自身のimportも含まれるため、
  長さだけでは原因を確定できない。`uvx`へ`-v`を付けて再実行し、
  パッケージの取得と環境の作成を示す出力が現れるかを確かめる。現れれば原因2に当たる

### 原因1: 起動コマンドのパッケージ解決が失敗する

`uvx`は`--from`の指定を起動時に解決する。インデックス応答のキャッシュが有効な間は通信せずに済むが、
キャッシュが無い場合や有効期限を過ぎた場合はPyPIへ問い合わせる。
この問い合わせが失敗すると`uvx`は非0で終了し、
標準出力へ何も書かないため初期化応答が返らない。
断続的に発生するのは、通信を要する起動と要さない起動が混在するためである。

通信の要否は版の指定方法に依存しない。
`--from "pyfltr==3.17.0"`のような完全一致指定でも、インデックス応答のキャッシュが無ければ問い合わせが発生する。

対処は、pyfltrをツール環境へ事前に導入し、起動のたびにパッケージを解決しない形へ変更することである。
通信できる状態で次のコマンドを実行し、実行ファイルの格納先を確認する。

```sh
uv tool install "pyfltr==3.17.0"
uv tool dir --bin
```

MCPクライアントの`command`には、表示されたディレクトリ内にあるpyfltr実行ファイルの絶対パスを指定する。
Windowsでは実行ファイル名が`pyfltr.exe`になる。

```json
{ "mcpServers": { "pyfltr": {
    "command": "/absolute/path/to/uv-tool-bin/pyfltr",
    "args": ["mcp"] } } }
```

事前導入後の直接起動はインデックス応答のキャッシュを参照せず、起動時の通信を必要としない。
更新は`uv tool install --force "pyfltr==<更新先の版>"`で明示的に実施する。

### 原因2: 環境の構築を伴う起動が待ち時間を超過する

`uvx`は解決した版の環境がキャッシュに無い場合、依存パッケージの取得と環境の構築を済ませてから
pyfltrを起動する。このときの所要時間は回線速度とキャッシュの状態に応じて変動し、
通常の起動より大きく延びることがある。
クライアント側の起動待ち時間の既定値を超えると、その回だけ起動に失敗する。
この場合のエラーは接続断ではなく待ち時間の超過を示す文言になる。

範囲指定を使っている場合は、新しいpyfltrが公開された直後の起動が該当しやすい。
解決先が新しい版へ移り、その版の環境を構築するためである。
版を固定している場合も、キャッシュを削除した後の初回起動は同じ経路を通る。

環境の構築が実行されているかは`uvx -v`の出力で確かめる。
構築済みの環境で起動した場合は解決結果の報告だけが出るが、
構築が実行される場合はパッケージの取得と導入を示す行が加わる。

対処は次のいずれかとする。

- `uv tool install`でpyfltrを事前に導入し、原因1の手順どおり実行ファイルを直接起動する。
  ツール環境が保持されるため、MCPクライアントの起動時に環境の構築は発生しない

- 登録しているコマンドを手元で1回実行し、クライアントの起動より先に環境の構築を済ませておく

  ```sh
  uvx --from "pyfltr>=3.16" pyfltr --version
  ```

- クライアント側の起動待ち時間を延ばす。codexでは`~/.codex/config.toml`へ次を記述する

  ```toml
  [mcp_servers.pyfltr]
  startup_timeout_sec = 60
  ```

版指定の固定は、範囲指定が新しい版へ自動的に切り替わって環境を再構築する事態を避ける。
一方、キャッシュそのものが無い状態では固定した版でも構築が実行されるため、
原因2の全体を解消する対処にはならない。

## pre-commit・prek統合時の自動スキップ

pre-commit・prek（以下いずれも実行系と呼ぶ）からpyfltrを呼び出しているのに、一部のツールが実行されないことがある。
これは実行系を経由した起動時の意図的なフィルタリング動作である。

pyfltrは実行系から呼び出されたことを環境変数`PRE_COMMIT=1`で検出する。
pre-commitとprekは、いずれもこの環境変数を設定する。
`PRE_COMMIT=1`が設定されている場合、`pyfltr fast`サブコマンドは`{command}-fast = true`のツールのみを対象として実行する。
`run`サブコマンドは自動スキップを行わないため、`fast`を指定している場合は意図した動作となる。

確認方法。

- 実行系の`entry`設定が`pyfltr fast`になっているか確認する
- `{command}-fast`の設定を`pyproject.toml`で確認する
 （既定では重いツール、mypy・pylint・pytestなどはfastに含まれない）
- `pyfltr fast --verbose`で実行対象コマンドの一覧を確認する

`make test`等から`pyfltr run`を呼び出すと、pyfltrは`SKIP=pyfltr`付きで有効な実行系を
変更ファイル指定（`--files <対象>`）で起動する。
この自動連携を抑止する場合は`pre-commit-auto-skip = false`を設定する。
prekでは`prek-auto-skip = false`を設定する。

既定設定では`--files`で対象ファイルを渡して起動するため、
引数なしの実行系が行う未ステージ変更の退避・復元（`git stash`相当の作業ツリー操作）は発生しない。
対象ファイルが0件の場合は実行系自体を起動しない。

## 起点ディレクトリ外のファイルが検査されない場合

起点ディレクトリの外にある絶対パスを指定すると、
`{command}: 起点cwd外のパスは対象から除外しました: {path}`という警告が出て
`markdownlint`・`textlint`・`prek`等が実行されないことがある。

これらのツールは設定ファイルの解決やリポジトリ単位の走査を前提とするため、
既定では起点ディレクトリ外のパスを対象から除外する。

起点外のファイルを検査したい場合は`--allow-external-paths`を指定する。

```sh
pyfltr run --allow-external-paths --commands=textlint,markdownlint ~/.claude/plans/example.md
```

`markdownlint`・`textlint`の設定ファイルは起点ディレクトリ直下から解決して`--config`で
明示的に渡すため、起点外のファイルにもプロジェクトの設定が適用される。
`pytest`等のテスト実行ツールへ起点外パスが渡る構成では想定外の動作になり得るため、
`--commands`で対象ツールを限定して実行する。

## `--changed-since`で対象ファイルが空になる場合

`--changed-since <REF>`を指定したのに実行対象が0件になることがある。

考えられる原因と確認手順は以下のとおり。

1. 指定した`<REF>`との間に差分がない場合。
   `git diff --name-only <REF>`のコミット差分・trackedファイルの作業ツリー差分・staged差分の和集合と
   ファイル展開の時点で残ったファイルリストとの交差が空集合になる。
   `git diff --name-only <REF>`を直接実行して変更ファイルの一覧を確認する。
2. 対象ファイルが`exclude`や`.gitignore`で除外されている場合。
   ファイル展開の時点で除外されたファイルは`--changed-since`フィルタの前段で既にリストから除外される。
   `--no-exclude`や`--no-gitignore`を付けて確認できる。
3. `<REF>`にtrackedファイルの作業ツリー差分・staged差分だけを含めたい場合。
   `--changed-since=HEAD`は`HEAD`との差分（trackedファイルの作業ツリー差分とstaged差分）を対象とする。
   HEADを含む過去コミットとの比較をしたい場合は`HEAD~1`や具体的なコミットハッシュを指定する。
   なお、untrackedの新規ファイル（`git add`未実施）は`git diff`の出力に含まれないため対象外となる。

gitが不在またはrefが存在しない場合は警告を出力して全体実行へフォールバックする。
`pyfltr run --verbose --changed-since=<REF>`を実行すると警告メッセージを確認できる。

CI上で`origin/main`等のリモート追跡refを指定して毎回全体実行になる場合は、チェックアウトが浅い可能性がある。
`actions/checkout`の既定は`fetch-depth: 1`で、リモート追跡refはトリガーとなったref分だけを生成する。
pull_requestイベントでは`refs/remotes/pull/`配下だけを生成するため、`fetch-depth: 0`の指定が必要となる。

## `--only-failed`が想定どおり動かない場合

`--only-failed`で再実行しているのに失敗ツールが拾われない、または全体実行になることがある。

主な原因と対処。

- 直前runのアーカイブが残っていない（`--no-archive`または`archive = false`で記録されなかった）。
  通常実行に戻して再度`--only-failed`を試す
- 直前runで失敗ツールが0件だった。`pyfltr show-run latest`でそのrunのステータスを確認する
- 位置引数（targets）と直前runの失敗ファイル集合の交差が空になっている。
  位置引数を外す、または対象ディレクトリを広げる
- `pass-filenames=False`のツール（tsc・cargo-\*・dotnet-\*等）で全体失敗のみだった場合、診断ファイルが
  取得できないため既定対象でフォールバック実行する。これは仕様である
- `--from-run <RUN_ID>`に存在しないrunを指定した場合は警告を出力してrc=0で早期終了する。
  `pyfltr list-runs`で実在のrun_idを確認する

補足。`--only-failed`はファイル変更検出・テスト関数の存在確認・モジュール依存追跡を行わない。
判定基準は直前runの診断ファイル一覧と現在の`targets`集合の交差のみで、ファイル変更ベースのフィルタリングは`--changed-since`との併用で実現する。
ツール単位で`status=skipped, files=0`を観測した場合は`pyfltr show-run latest`で当該ツールの診断ファイル有無を確認する。
診断ファイルなしの場合はフォールバック実行が対象`globs`に一致せず、対象ファイル0件で終了する。
診断ファイルありの場合はツール単位のINFOログを次の例の形式で出力するため、除外理由を判定できる。

```text
--only-failed: <tool>: 直前 run の失敗ファイル N 件は指定 targets と交差しません。本ツールは対象から除外します。
```

## mise関連のトラブル {#mise-troubleshooting}

`bin-runner = "mise"`（既定）でcargo系・dotnet系・shellcheck等のツール実行が失敗する場合の対処。

### `mise.toml`未信頼で失敗する

worktreeやdotfiles配下では`mise.toml`が未信頼扱いとなり`mise exec`が失敗することがある。

- 既定では`mise-auto-trust = true`が`mise trust --yes --all`を自動実行する。
  `--all`はcwdおよび親ディレクトリ全configを信頼するため、プロジェクト外のmise.tomlも対象になる点に注意
- 自動信頼を無効化したい場合は`mise-auto-trust = false`を設定し、手動で`mise trust`を実行する

### `mise install`が失敗する

ネットワーク制約・プラットフォーム未対応などで`mise install`が失敗する場合がある。

- `mise install`を手動で実行してエラー内容を確認する
- 該当ツールに`{command}-runner = "direct"`を設定してPATH直接実行へ戻す
- バージョン指定（`{command}-version`）を変更して入手可能なバージョンを使う

### `bin-runner = "mise"`でもdirectで起動される

`bin-runner = "mise"`にしているのに`pyfltr command-info`の`effective_runner`が`direct`になることがある。
これは「miseバイナリがPATH上に存在しない場合のみ」発火する救済挙動で仕様どおり。
mise本体を導入すれば自動的にmise経由起動へ切り替わる。

mise本体は存在するが`mise exec`が失敗する場合（バージョン解決失敗・config未信頼など）は
directにフォールバックせず`failed`として扱う。

### `pyfltr command-info <tool>`での確認

ツールがどの経路で起動されるかは`pyfltr command-info <tool>`で確認できる。
`runner` / `effective_runner` / `executable` / `commandline`を見れば、`{command}-runner`設定や
グローバル`bin-runner`の効果が想定どおりかが分かる。
`--check`オプションを付けると、実行時と同じ事前チェック（mise経由ツールの`mise exec --version`と、
パッケージマネージャー系ツールの最低版確認）まで実施する。`mise install`や`--version`の起動が発生する場合がある。

## PATHが重複していてmiseのtools解決が有効にならない

`mise exec`が想定外の経路でツール解決する場合は次を試す。

- `mise --version`でmise本体が更新済みか確認する
- `pyfltr command-info <tool> --check`で実際の起動コマンドラインを観察する
- ユーザーシェル設定で`PATH`に`mise/installs/`配下を直接追加していないか確認する

## uvx pyfltr単独実行時にpylintのimport-errorが出る

`uvx pyfltr`単発で実行した場合、cwdに`uv.lock`がある環境では
利用者プロジェクトのvenv経由で起動されるため、pylintが`import-error`を発行するケースがある。

対処は次のいずれかを選ぶ。

- 利用者プロジェクトのdev依存にpyfltr本体を加える（推奨）。
  `uv add --dev "pyfltr[python]"`を実行して`uv run pyfltr ...`で呼び出す
- もしくは`pylint-runner = "direct"`を`pyproject.toml`の`[tool.pyfltr]`配下に明示する

呼び出し方の使い分けと推奨理由は[呼び出し方の使い分け](recommended.md#calling-style)を参照。

## 実行アーカイブのディスク使用量確認（定期管理）

pyfltrは各実行の結果をユーザーキャッシュ配下にアーカイブとして保存する。
長期間使用するとディスク使用量が増加する場合があるため、定期的な確認手順を以下に示す。

`pyfltr list-runs`で実行アーカイブの一覧を確認できる。

```shell
# 直近20件を一覧表示（既定）
pyfltr list-runs

# 件数を増やして確認
pyfltr list-runs --limit=100

# JSONL形式で詳細確認
pyfltr list-runs --output-format=jsonl
```

一覧には`RUN_ID`・`STARTED_AT`・`EXIT`・`FILES`・`COMMANDS`が表示される。
アーカイブが存在しない環境では`(no runs)`を出力する。

アーカイブの実体は次の場所に保存される。

| OS | 保存先 |
| --- | --- |
| Linux | `~/.cache/pyfltr/` |
| macOS | `~/Library/Caches/pyfltr/` |
| Windows | `%LOCALAPPDATA%\pyfltr\Cache` |

手動で削除する場合はそのディレクトリを対象にする。

アーカイブを無効化する場合は`--no-archive`オプションまたは
`pyproject.toml`の`archive = false`設定を使う。

## 設定キーのtypoと未知キー

`pyproject.toml`の`[tool.pyfltr]`に未知の設定キーを書いた場合、
pyfltrは原因と対処を1つのエラーメッセージにまとめて返す。

```text
設定キー `lychee` は認識できません。もしかして: lychee-args, lychee-runner, lychee-fast。
有効なキー一覧は `pyfltr config list --all` で確認できます
```

対処方法。

- 表示されたサジェストの中に意図したキーがあればそれへ書き換える
- サジェストが無い場合は`pyfltr config list --all`で全キー一覧を確認する。
  当該一覧上で`(default)`の注記が付くキーは既定値で動作中であり、上書きしたい場合のみpyproject.tomlへ追記する
- global設定（`~/.config/pyfltr/config.toml`）の未知キーはValueErrorではなく警告で無視される。
  古いpyfltrで新版設定を読み込んでも停止させないための前方互換の挙動である

v3.0.0で削除されたツール（pyupgrade・autoflake・isort・black・pflake8）の
設定キーを残している場合は移行案内付きの`ValueError`で停止する。
ruffへ統合済みのため当該設定をすべて削除する。

## ツール解決失敗時の対処

`pyfltr run`実行時に`resolution_failed`扱いで停止する場合、ツールカテゴリ別に対処を選ぶ。

### Python系ツール（mypy・pylint・pyright・ty・pytest・ruff-format・ruff-check・uv-sort・bandit）

メッセージ例。

```text
ツールが見つかりません: Python系ツール `mypy` が PATH 上にありません。
`mypy-runner = "uv"`（cwdに uv.lock が必要、`uv add --dev "pyfltr[python]"` で依存追加）
または `mypy-runner = "uvx"` への切り替え、もしくは `mypy-path` で実行ファイルを明示してください
```

対処方法。

- `{command}-runner = "uv"`へ切り替える。
  プロジェクト直下に`uv.lock`が必要で、`uv add --dev "pyfltr[python]"`で依存追加する
- `{command}-runner = "uvx"`へ切り替える。
  PyPI最新版をその都度取得する（`uv.lock`は参照せず、再現性は犠牲になる）
- `{command}-path = "/path/to/bin"`で実行ファイルを直接指定する

### uvx既定のPython製ツール（semgrep・sqlfluff）

semgrep / sqlfluffは本体依存から分離され、既定の`{command}-runner = "uvx"`で別環境へ解決する。
`uvx`が利用できず、対象ツールの実行ファイルもPATH上にない場合は`resolution_failed`になる。

対処方法。

- `uv tool install semgrep`または`uv tool install sqlfluff`で独立環境へ導入する。
  `uvx`は導入済み環境を再利用するため、実行のたびの解決も版の揺れも避けられる
- `uv add --dev semgrep`または`uv add --dev sqlfluff`で利用者プロジェクトへ個別に追加し、
  `{command}-runner = "direct"`へ切り替える
- `{command}-path = "/path/to/bin"`で実行ファイルを直接指定する

uv / uvx経路の実行後に未登録エラーが出た場合は、`uvx semgrep`または`uvx sqlfluff`で直接実行するか、
`{command}-runner = "direct"`へ切り替える。

### JS系ツール（textlint・markdownlint・eslint・biome・oxlint・prettier・tsc・vitest・designmd）

メッセージ例。

```text
js-runner=direct で `textlint` がローカル node_modules に見つかりません
（探索先: node_modules/.bin/textlint）。`pnpm install` などで対象パッケージを導入するか、
`js-runner = "pnpx"` でグローバルキャッシュ経由に切り替えてください
```

対処方法。

- `pnpm install`等でローカル`node_modules`へ対象パッケージを導入する
- `js-runner = "pnpx"`へ切り替えてpnpmグローバルキャッシュ経由で起動する。
  この経路は`node_modules`を必要としない
- `{command}-path = "..."`で実行ファイルを直接指定する

### ネイティブ系ツール（shellcheck・shfmt・cargo-*・dotnet-*・lychee・taplo・hadolint等）

メッセージ例。

```text
ツールが見つかりません: `cargo-clippy` が解決できません。
`cargo-clippy-runner = "direct"` への切り替えか、
`cargo-clippy-path` で実行ファイルを明示してください
```

対処方法。

- `mise install`で対応ツールを導入する（既定の`bin-runner = "mise"`経路）
- `{command}-runner = "direct"`へ切り替えてPATH上のバイナリを直接実行する
- `{command}-path = "/path/to/bin"`で実行ファイルを直接指定する

mise経由で発生する未信頼エラー・registry解決失敗等の追加対処は
[mise関連のトラブル](#mise-troubleshooting)節を参照。

## `--commands`で指定したコマンドが実行されない {#commands-not-run}

サブディレクトリで実行した場合や`pyproject.toml`で対象コマンドが有効化されていない場合、
指定した`--commands`のうち一部が実行されずexit 0で終了することがある。
実行されなかったコマンドは警告レコード（`source: "commands"`）で明示される。
`--enable=<コマンド名>`または`pyproject.toml`の`[tool.pyfltr]`で当該コマンドを`true`に設定して有効化する。
サブディレクトリで実行している場合は`pyproject.toml`を持つプロジェクトルートで実行することも検討する。

## モノレポでサブプロジェクト分割が想定通り動かない場合

- サブプロジェクトが検出されない
    - 起点cwd配下にマーカー（`pyproject.toml`・`Cargo.toml`・`*.csproj`・`*.sln`）を1件しか
      検出していない場合、モノレポモードは適用されない（単一プロジェクトとして従来通り動作）
    - `.gitignore` に記載されたディレクトリは除外される。検出させたい場合は `.gitignore` の見直しか
      `subproject-use-gitignore = false` の設定で除外を解除する
    - `.venv`・`node_modules`・`target`・`build`・`dist`・`.git` は既定で常に除外する。
      除外対象を追加・変更したい場合は `subproject-exclude` で名前を指定する
- 特定ツールをサブプロジェクト分割の対象から外したい
    - `{command}-subproject-aware = false` を `pyproject.toml` に追加する。
      リポジトリ単位で1回だけ起動する形に切り替わる
- `--work-dir` 指定時の探索起点
    - `--work-dir` 適用後のcwdを起点としてサブプロジェクトを検出する。
      `pyfltr run --work-dir=foo` なら `foo/` 以下を再帰探索する

## テストが通るのにpytestの設定が反映されない場合

pytest 9.0以降は設定ファイルを`pytest.toml`・`.pytest.toml`・`pytest.ini`・`.pytest.ini`・
`pyproject.toml`・`tox.ini`・`setup.cfg`の順で探索し、最初に見つかったものだけを採用する。
採用したものより後ろの候補は、設定が書かれていても丸ごと無視される。
無視された側にタイムアウト・並列実行・収集除外・マーカー登録を書いていると、
いずれも適用されないままテストが完走し終了コード0で終わる。

8系以前は探索順に`pytest.toml`・`.pytest.toml`を含まず、後続候補を無視する際の通知も出力しない。
無視される挙動自体は同じであるため、本節の警告と確認手順は9.0以降でのみ利用できる。
8系以前では設定を書いたファイルが採用されているかを`configfile:`行で直接確かめる。

pyfltrは当該の競合を検出すると次の警告を発行する。ツールの成否は変えない。

```text
pytest: 設定ファイルが競合しています。採用: pytest.ini / 無視: pyproject.toml
```

対処方法。

- 採用された設定ファイルへ設定を集約するか、不要な設定ファイルを削除する
- 現在どの設定ファイルが採用されているかは`pytest --collect-only`の`configfile:`行で確認する。
  競合がある場合は同じ行へ`(WARNING: ignoring pytest config in ...)`が付く
- `-q`・`--no-header`を指定するとヘッダー行そのものが出力されないため、
  当該警告も出なくなる。確認時はこれらを外して実行する
