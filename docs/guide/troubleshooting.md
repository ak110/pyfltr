# トラブルシューティング

## MCP起動時のstdout占有事故

`pyfltr mcp`起動後に他のコマンド出力やlogが端末に表示されず、
コーディングエージェントに返る結果が文字化けするまたは解析エラーになる場合がある。

`pyfltr mcp`を起動するとstdin/stdoutがJSON-RPCフレームに専有される。
同一プロセスや同一パイプライン内で他のテキスト出力が混入すると、
JSON-RPCパーサーがフレームを正しく解析できなくなる。

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
- `command`に絶対パスではなく`uvx`を使い、シェル解決に依存させない
- クライアント側のMCPサーバーログ（Claude Codeであれば設定UIから確認可能）でJSON-RPCエラーの詳細を確認する
- stdoutに非JSON出力が混じっていないかをstderrへリダイレクトして確認する
 （`uvx pyfltr mcp 2>/tmp/pyfltr-mcp.err`のように手動起動して観察できる）

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

逆方向として、`make test`等から`pyfltr run`を呼び出した場合は、pyfltr側が`SKIP=pyfltr`付きで
`pre-commit run --all-files`を起動して二重実行を避ける。
この自動連携を抑止したい場合は`pre-commit-auto-skip = false`を設定する。

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

gitが不在またはrefが存在しない場合は警告を出して全体実行へフォールバックする。
`pyfltr run --verbose --changed-since=<REF>`を実行すると警告メッセージを確認できる。

## `--only-failed`が想定どおり動かない場合

`--only-failed`で再実行しているのに失敗ツールが拾われない、または全体実行になることがある。

主な原因と対処。

- 直前runのアーカイブが残っていない（`--no-archive`または`archive = false`で記録されなかった）。
  通常実行に切り戻して再度`--only-failed`を試す
- 直前runで失敗ツールが0件だった。`pyfltr show-run latest`でそのrunのステータスを確認する
- 位置引数（targets）と直前runの失敗ファイル集合の交差が空になっている。
  位置引数を外す、または対象ディレクトリを広げる
- `pass-filenames=False`のツール（pytest等）で全体失敗のみだった場合、診断ファイルが取得できないため
  既定対象でフォールバック実行する。これは仕様
- `--from-run <RUN_ID>`に存在しないrunを指定した場合は警告を出してrc=0で早期終了する。
  `pyfltr list-runs`で実在のrun_idを確認する

## mise関連のトラブル

`bin-runner = "mise"`（既定）でcargo系・dotnet系・shellcheck等のツール実行が失敗する場合の対処。

### `mise.toml`未信頼で失敗する

worktreeやdotfiles配下では`mise.toml`が未信頼扱いとなり`mise exec`が失敗することがある。

- 既定では`mise-auto-trust = true`が`mise trust --yes --all`を自動実行する。
  `--all`はcwdおよび親ディレクトリ全configを信頼するため、プロジェクト外のmise.tomlも対象になる点に注意
- 自動信頼を無効化したい場合は`mise-auto-trust = false`を設定し、手動で`mise trust`を実行する

### `mise install`が失敗する

ネットワーク制約・プラットフォーム未対応などで`mise install`が失敗する場合がある。

- `mise install`を手動で実行してエラー内容を確認する
- 該当ツールに`{command}-runner = "direct"`を設定してPATH直接実行へ切り戻す
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
`--check`オプションを付けると`mise exec --version`の事前チェック（`mise install`が起動する場合あり）まで実施する。

## PATHが重複していてmiseのtools解決が効かない

cli起動時にPATHの重複エントリは順序先勝ちで自動整理される。
mise経由のsubprocessにはmiseが注入したtoolパスを除外したPATHを渡し、tools解決をスキップさせない仕組みが組み込まれている。
通常は利用者側の追加対応は不要。

それでも`mise exec`が想定外の経路でツール解決する場合は次を試す。

- `mise --version`でmise本体が更新済みか確認する
- `pyfltr command-info <tool> --check`で実際の起動コマンドラインを観察する
- ユーザーシェル設定で`PATH`に`mise/installs/`配下を直接追加していないか確認する
 （pyfltrは内部でこれを除外するが、ユーザー操作で再注入されると影響を受ける）

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

アーカイブの実体は次の場所に保存される。

| OS | 保存先 |
| --- | --- |
| Linux | `~/.cache/pyfltr/` |
| macOS | `~/Library/Caches/pyfltr/` |
| Windows | `%LOCALAPPDATA%\pyfltr\Cache` |

手動で削除する場合はそのディレクトリを対象にする。

アーカイブを無効化したい場合は`--no-archive`オプションまたは
`pyproject.toml`の`archive = false`設定を使う。
