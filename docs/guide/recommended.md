# 推奨設定例

Pythonプロジェクト向けの推奨構成例（pyproject.toml・prek・タスクランナー・CI設定）。
設定して実行するところから始める場合は[はじめに](getting-started.md)を参照。

## pyproject.toml

pyfltr本体の設定（`[tool.pyfltr]`）と、呼び出される各ツール（ruff / mypy / pytest）の設定を1つの`pyproject.toml`にまとめた例。

- `preset = "latest"`: 各時点での推奨ツール構成。詳細は[プリセット設定](configuration.md#preset)を参照
- `python = true`: Python系ツールのゲートを開ける。推奨ツール（ruff-format / ruff-check / mypy /
  pylint / pyright / pytest / uv-sort）を一式有効化する
    - Python系ツール一式は本体依存に同梱されているため、`uvx pyfltr`単発で利用できる
    - dev依存に固定する場合は`uv add --dev "pyfltr[python]"`（pip環境では`pip install pyfltr`）を使う
- `pylint-args`: pylintに追加で渡す引数。`--load-plugins=pylint_pydantic`と
  `--enable-error-code=unused-awaitable`（mypy）は自動オプションで既定有効のため個別指定不要
- `[tool.pylint."messages control"]`: pylintのdisableリストを`pyproject.toml`に集約することで、
  `.pylintrc`を別途配置する必要がなくなり設定の所在が`pyproject.toml`1ファイルにまとまる
    - ruffの`D`カテゴリが`missing-*-docstring`相当を検出するため、
    pylint側の同系ルールは無効化しても品質低下は招かない
    - `missing-class-docstring`はテストクラスに対する儀礼的docstring付与を無効化する
    - `missing-module-docstring`は`__init__.py`等でモジュールdocstring要求を緩和する目的で無効化する
- `[tool.pylint."messages control"]`のdisableリストから`"duplicate-code"`（R0801）を除去し重複検出を有効化する。
  `[tool.pylint.similarities]`は有効化済みの検査に対し`min-similarity-lines = 10`で閾値を設定する
    - 閾値10は4プロジェクトの既存コード計測に基づき、ノイズ抑制と実重複の捕捉を両立する規模として選定した
    - 選定時の計測では、検出のあったプロジェクトで閾値4の検出件数が閾値10の場合の数倍から十数倍に達し、
      閾値10ではいずれも数十件以下に収まった。両閾値とも0件のプロジェクトもあった
    - 検出件数はコードベースの内容とpylintの版で変動する。
      閾値を自プロジェクト向けに見直す場合は手元で計測し直す
    - 検出時は実重複か否かを判別する。実重複であれば共通化のリファクタリングを第一候補とし、
      意図的な並行実装や共通化すべきでない類似は理由コメント付き`# pylint: disable=duplicate-code`で個別抑制する。
      disableリストへの再追加や根拠を示さない閾値変更はしない
    - `pylint-args`へ`--jobs=4`を指定した状態で`duplicate-code`を有効化すると、
      同一の重複箇所に対して報告されるファイルの組が`--jobs=1`の場合と一致しないことがある。
      重複はモジュール横断で集約する検査であり、並列実行では分割されたプロセス単位で
      代表となる組が選ばれるためである。
      重複箇所を特定する場合は`--jobs=1`で再実行する
- ruffの `per-file-ignores`: テストコード（`**_test.py`）とpackage init（`__init__.py`）のdocstring要求を除外する実用的な調整

`uvx pyfltr`での実行では`pyproject.toml`にpyfltrを記述する必要はなく、`[tool.pyfltr]`セクションのみで完結する。
dev依存に固定する場合のみ`[dependency-groups] dev`に`"pyfltr[python]"`を追加する（後置の併記例）。

```toml
[tool.pyfltr]
preset = "latest"
python = true
pylint-args = ["--jobs=4"]

[tool.pylint."messages control"]
disable = [
    "broad-exception-caught",
    "fixme",
    "invalid-name",
    "line-too-long",
    "logging-fstring-interpolation",
    "logging-not-lazy",
    "missing-class-docstring",
    "missing-function-docstring",
    "missing-module-docstring",
    "no-else-return",
    "too-few-public-methods",
    "too-many-arguments",
    "too-many-boolean-expressions",
    "too-many-branches",
    "too-many-instance-attributes",
    "too-many-lines",
    "too-many-locals",
    "too-many-nested-blocks",
    "too-many-positional-arguments",
    "too-many-public-methods",
    "too-many-return-statements",
    "too-many-statements",
]

[tool.pylint.similarities]
min-similarity-lines = 10

[tool.ruff]
# https://docs.astral.sh/ruff/configuration/
line-length = 128

[tool.ruff.lint]
# https://docs.astral.sh/ruff/linter/#rule-selection
select = [
    # pydocstyle
    "D",
    # pycodestyle
    "E",
    # Pyflakes
    "F",
    # pyupgrade
    "UP",
    # flake8-bugbear
    "B",
    # flake8-simplify
    "SIM",
    # flake8-import-conventions
    "ICN",
    # isort
    "I",
]
ignore = [
    "D107", # Missing docstring in `__init__`
    "D415", # First line should end with a period
    "D403", # First word of the first line should be properly capitalized（日本語docstringにそぐわないため）
]

[tool.ruff.lint.pydocstyle]
convention = "google"

[tool.ruff.lint.per-file-ignores]
"**_test.py" = ["D"]
"**/__init__.py" = ["D104"]  # Missing docstring in public package

[tool.mypy]
# https://mypy.readthedocs.io/en/stable/config_file.html
allow_redefinition = true
check_untyped_defs = true
ignore_missing_imports = true
strict_optional = true
strict_equality = true
warn_no_return = true
warn_redundant_casts = true
warn_unused_configs = true
show_error_codes = true

[tool.pytest.ini_options]
# https://docs.pytest.org/en/latest/reference/reference.html#ini-options-ref
addopts = [
    "--showlocals",
    "-p", "no:cacheprovider",
    "--maxfail=5",
    "--durations=30",
    "--durations-min=0.5",
    "--timeout=60",
    # コア数追従（-n auto 等）を採用せず固定値とする（CI環境での逆効果とメモリ消費増を避けるため）
    "-n", "4",
    # 未実行テストを動的分配し、特定ファイルへの偏りによる律速を回避する
    "--dist=worksteal",
]
log_level = "DEBUG"
xfail_strict = true
asyncio_mode = "strict"
asyncio_default_fixture_loop_scope = "session"
asyncio_default_test_loop_scope = "session"
filterwarnings = [
    "error::ResourceWarning",
    "error::pytest.PytestUnraisableExceptionWarning",
    "error::RuntimeWarning",
]
```

`[tool.mypy]`のうち`ignore_missing_imports`と`allow_redefinition`は検査を緩和する指定である。
`ignore_missing_imports = true`は解決できないimportのエラーを抑止するため、
型スタブの欠落や誤ったモジュール名を検出しない。
`allow_redefinition = true`は注釈のない変数を無関係な型で再定義することを許容するため、
変数の使い回しによる型の取り違えを検出しない。
いずれも検査は通過するが欠陥は残りうる。厳格さを優先する場合は`false`とする。

`asyncio_default_fixture_loop_scope`・`asyncio_default_test_loop_scope`の`"session"`は、
pytest-asyncioの既定（それぞれfixtureスコープ・functionスコープ）より分離を弱める指定である。
上記の例は`-n 4`でpytest-xdistの並列実行を行うため、workerごとに別プロセスのevent loopとなる。
同一worker内では割り当てられたテストが1つのevent loopを共有し、
loopを閉じるテストや解放されない非同期資源は同じworkerの後続テストへ波及する。
テスト間の独立性を優先する場合は、両設定へ`"function"`を明示指定する。
`asyncio_default_fixture_loop_scope`を未設定にすると、pytest-asyncioが非推奨警告を送出する。
この警告が表示されるかはpytest本体の版で決まり、pytest 8.3.5では表示され、8.4.0以降は表示されない。
pytestがconfig段階の警告収集コンテキストを`record=False`で開くためである。
コマンドラインの`-W always`では復活せず、環境変数`PYTHONWARNINGS=always`を与えた場合のみ表示される。
警告の送出自体は版によらず続くため、設定を削除せず値を変更する。

`-p no:cacheprovider`はpytestのcacheproviderプラグインを無効化する。
このプラグインは`--lf`・`--ff`・`--nf`・`--lfnf`・`--cache-show`・`--cache-clear`の各オプションと、
ini設定`cache_dir`および`cache` fixtureを提供する。
stepwiseプラグインは`--sw`・`--sw-skip`・`--sw-reset`を提供する。
pytestはcacheproviderを無効化した場合、これに依存するstepwiseも連動して無効化する。
上記の例のように`addopts`へ書いた場合、両プラグインが提供する上記のオプションは引数として受理されるが効果を持たない。
`--lf`を指定しても対象は全件実行となり、`--cache-show`を指定してもキャッシュ内容を表示せずテストを実行する。
pytestは組み込みプラグインを読み込むかどうかをコマンドラインの`-p`指定だけで決めるため、
`addopts`へ書いた無効化は読み込みより後に評価される。
このとき無効化が取り消すのはプラグインの登録だけで、定義済みのオプションは残る。
コマンドラインで`-p no:cacheprovider`を直接渡した場合は、読み込みより前に評価されるため
オプションが定義されず、`--lf`等を併せて指定すると`unrecognized arguments`でエラー終了する。
この差は組み込みプラグインに限る。`pytest-xdist`のように外部パッケージとして読み込むプラグインは
`addopts`の評価より後に読み込まれるため、`addopts`へ書いた無効化でもオプションが定義されない。
`cache` fixtureを要求するテストは、`addopts`へ書いた場合は`AttributeError`で、
コマンドラインで渡した場合はfixture不存在で、いずれも失敗する。
これらを利用する場合は当該指定を外す。

`filterwarnings`の3件はテストの資源解放漏れを検出する指定である。
ファイルやソケットを閉じないままGCされると`ResourceWarning`が、
awaitされないまま破棄されたコルーチンでは`RuntimeWarning`が送出される。
いずれもGC時の`__del__`内で送出されるため、エラー化した結果は例外として送出できない。
pytestのunraisableexceptionプラグインがこれを`PytestUnraisableExceptionWarning`へ包み直す。
テストの成否を決めるのは包み直した後の当該の警告であり、
`ResourceWarning`・`RuntimeWarning`だけをエラー化しても失敗しない。
3件は相互に依存し、いずれを欠いても検出できない。
全警告をエラー化する`filterwarnings = ["error"]`でも検出できるが、
外部ライブラリの`DeprecationWarning`まで失敗へ変えるため、除外エントリの継続的な保守を要する。
上記の3件へ限定すると、エラー化の対象は資源解放と非同期呼び出しの取りこぼしに限られる。
依存ライブラリが同じカテゴリの警告を送出する場合は当該のテストも失敗する。
導入時はまず全件を実行し、失敗するテストの警告の発生元を確認する。
自プロジェクトの解放漏れはテスト側の資源解放で是正し、
依存ライブラリ由来のものは`ignore`エントリで個別に除外する。
pytestは`filterwarnings`を後勝ちで適用するため、`ignore`エントリは上記の3件より後ろへ置く。

`--timeout=60`は`pytest-timeout`プラグインが必要。
値60は個々のテストが1分以内に完了する前提に基づく。
統合テストや低速なCIランナーでは間欠失敗の原因となるため、
個別のテストで上書きする場合は`@pytest.mark.timeout(秒数)`を、無効化する場合は`@pytest.mark.timeout(0)`を指定する。
マーカーは`--timeout`より優先される。

`-n 4`は`pytest-xdist`プラグインが必要で、4プロセス並列でテストを実行する。
コア数追従（`-n auto`等）は採用せず固定値とする（CI環境で逆効果になり得ることと、メモリ消費の増大を避けるため）。
`--dist=worksteal`は`pytest-xdist`のwork-stealingスケジューラを有効化し、
テストファイル間の実行時間差が大きい場合でも特定workerへ偏らせない。
ランナー別の有効条件は次の通り。

- `python-runner = "direct"`経路では、pyfltrのvenv配下のpytestを直接起動するため
  本体依存の`pytest-timeout`・`pytest-xdist`がそのまま利用できる
- `uvx pyfltr`を`uv.lock`不在のディレクトリで実行する標準シナリオでは、`uv`経路の前提が満たされず
  `shutil.which`によるdirectフォールバックが発生する
    - この場合もpyfltrの本体venv同梱のpytestが採用されるため本体依存のプラグインが有効
- `uv`経路（既定）でcwdの`uv.lock`にpytestが登録されている場合は、
  利用者プロジェクトのvenvでpytestが解決される
    - プロジェクト側に`pytest-timeout`・`pytest-xdist`を導入する
    （`uv add --dev pytest-timeout pytest-xdist`等）
- `uvx`経路（per-tool直接指定でpytest用の独立環境が生成される場合）は、
  当該環境側への`pytest-timeout`・`pytest-xdist`の導入が別途必要
- pytest-xdistの並列実行下では、ポート番号・一時ファイル名・グローバル状態の競合に注意する
    - 間欠失敗するテストは並列前提に修正する。
      切り分けのため一時的に並列を止める場合は`-n 0`で`addopts`の並列度を上書きする
    - `-p no:xdist`はプラグイン自体を無効化するため`-n`・`--dist`が未定義となる。
      上記の例のように`addopts`へ両者を書いた構成では`unrecognized arguments`でエラー終了する
    - 各workerは自身へ割り当たったテストだけを実行するため、
    1つのモジュールが複数workerへまたがるとworkerごとにモジュールスコープのfixtureが実行される
    - 初期化を1回しか許さないプロセス内のグローバル状態は、
    モジュールスコープのfixtureで初期化せずsessionスコープへ移す
    - sessionスコープのfixtureもworkerごとに1回実行されるため、
    プロセス外の共有資源をworker間で1回だけ初期化する用途には別途排他が必要となる
    - モジュール単位で同一workerへ割り当てたい場合は`--dist=loadfile`・`--dist=loadscope`を選ぶ

### typosの許可語設定

プロジェクト固有の許可語がある場合は`pyproject.toml`の`[tool.typos]`セクションに追記する。
typos-cliは`pyproject.toml`の`[tool.typos]`を公式にサポートしているため、`_typos.toml`を別ファイルとして管理する必要はない。

```toml
[tool.typos.default.extend-words]
teh = "teh"
hte = "hte"
```

識別子（変数名・関数名）単位で許可したい場合は`[tool.typos.default.extend-identifiers]`を使う。
詳細は[typos公式ドキュメント](https://github.com/crate-ci/typos/blob/master/docs/reference.md)を参照。

### 依存の脆弱性監査の有効化（任意）

依存パッケージの脆弱性を`pyfltr`の枠組みでまとめて監査したい場合は`uv-audit`を有効化する。
`uv audit`（uv 0.11.2以降）が`pyproject.toml`を対象にPython依存の既知脆弱性を検査する。
サブコマンド自体は0.10.8で追加されたが、0.10.8・0.10.9は監査対象の件数を報告するのみで
脆弱性を検出しない。
0.10.10以降は検出するが、0.11.1までは検出時も終了コード0を返す。
非0の終了コードを返すのは0.11.2以降である
（[uv 0.11.2のリリースノート](https://github.com/astral-sh/uv/releases/tag/0.11.2)と
[uv PR #18512](https://github.com/astral-sh/uv/pull/18512)）。
pyfltrは終了コード0のツールを出力によらず成功として扱うため、0.11.2未満は実行前検査で拒否する。
0.10.10以上0.11.1以下で`uv-audit`を利用していた場合は、uvを0.11.2以降へ更新する。
更新しないまま実行すると解決失敗として扱われ、`uv-audit-severity`による警告への格下げもできない。
外部脆弱性データベースへ問い合わせるためネットワーク接続が必須で結果が変動する。
ネットワークが不安定なCIで失敗扱いを避けたい場合は`uv-audit-severity = "warning"`で警告扱いに切り替える。

```toml
[tool.pyfltr]
uv-audit = true
# ネットワーク不調時に失敗ではなく警告として扱う場合
# uv-audit-severity = "warning"
```

既定引数`uv-audit-args = ["audit", "--preview-features", "audit", "--frozen", "--no-progress"]`は
`--frozen`を含み、監査時に`uv.lock`を書き換えない。
`--preview-features audit`は`uv audit`が実験的機能である旨の警告を抑止する。

脆弱性監査の結果はコード変更と無関係に外部データベースの更新で変動する。
このためコミット毎やpre-commitではなく、日次・週次の定期実行に向く。
監査ツールのみをまとめて実行する場合は`--commands=audit`を指定する。
GitHub Actionsでは`schedule`トリガーの専用ワークフローへ切り出し、
通常のpush/PR用CIへ混在させない構成を推奨する。
SARIF出力（`--output-format=sarif`）と`github/codeql-action/upload-sarif`を
組み合わせると、GitHub Code Scanningにアラート管理を委ねられる。
同一の脆弱性は1件のアラートに集約され、解消後の定期実行で自動クローズされる。

この構成は公開リポジトリで利用できる。
非公開リポジトリでCode Scanningを使うにはGitHub Code Securityライセンスを要する
（[公式ドキュメント](https://docs.github.com/en/code-security/code-scanning/introduction-to-code-scanning/about-code-scanning)）。
非公開リポジトリでは、追加費用なく有効化できるDependabot alertsを脆弱性通知の主経路とする。
そのうえでSARIFはファイルへ出力し、監査ツールの実行有無と検出結果の判別に用いる。
`pyfltr`はツール実行の失敗をすべて終了コード1へ正規化するため、終了コードだけでは脆弱性の検出とツールの異常を区別できない。
SARIF内に当該ツールの`runs`要素が存在するかで無言スキップを検出する。
終了コードが非0の場合に限り、`results`が空かどうかで脆弱性の検出とツールの異常を区別する。
脆弱性を検出した場合はワークフローを失敗させて通知する。

### JS/TSを併用するプロジェクトでの推奨設定

JS/TSを併用するプロジェクトでは、`js-runner`をプロジェクトのパッケージマネージャーに合わせる。
既定の`pnpx`はツールを都度取得するため、CIで毎回ダウンロードが発生する。
`pnpm`や`npm`など、プロジェクトで使用しているパッケージマネージャーを指定すると、
`package.json`で管理済みのパッケージを再利用できる。

```toml
[tool.pyfltr]
js-runner = "pnpm"
```

`pnpm` / `npm` / `yarn` / `direct`では`textlint-packages`は無視される（`package.json`側でインストールする前提のため）。
textlintのプリセットやルールも`package.json`の`devDependencies`で管理する。

詳細は[設定項目（ツール別）](configuration-tools.md)の「js-runner経由で実行するツール」を参照。

### 日本語Markdownを含むプロジェクトでの推奨設定

日本語のMarkdownを含むプロジェクトでは`colloquial-check`を有効化する。
LLMが頻繁に出力する口語的な日本語表現を検出する内蔵linterで、既定では無効（opt-in）。

```toml
[tool.pyfltr]
colloquial-check = true
```

`colloquial-check-severity`の既定は`warning`のため、有効化してもCI・pre-commitは失敗しない。
既存の文書に口語表現が残っている場合は有効化した時点から警告が出るため、一度まとめて是正する。
検査対象の既定は全ファイル（`*`）であり、日本語Markdownだけでなくソースコードや設定ファイルに含まれる日本語も検査する。
辞書に一致する正当な用法を含むファイルは`colloquial-check-exclude`で除外し、
対象を特定の拡張子へ限定する場合は`colloquial-check-targets`を指定する。

詳細は[設定項目（ツール別）](configuration-tools.md)の「colloquial-check」を参照。

## .pre-commit-config.yaml

```yaml
default_language_version:
  python: python3

repos:
  - repo: https://github.com/pre-commit/pre-commit-hooks
    rev: v6.0.0
    hooks:
      - id: check-ast
      - id: debug-statements
  - repo: local
    hooks:
      - id: pyfltr
        name: pyfltr
        entry: uvx pyfltr fast
        types_or: [python, markdown, toml, yaml]
        require_serial: true
        language: system
```

注意: `default_language_version`にはプロジェクトが要求するPythonバージョンを指定する。
`python3`は実行環境が解決するPythonを使う指定であり、版を固定する場合は`python3.12`のように書く。
当該の指定が適用されるのは`language: python`のフックに限る。
上記の例では`pre-commit/pre-commit-hooks`側の2件が対象で、`language: system`のpyfltrは対象外である。
PEP 695型パラメーター構文（`def f[T](): ...`）を使用するプロジェクトではPython 3.12以上が必要となる。
指定した版が古いと、`check-ast`や`debug-statements`などPythonで実装されたフックがSyntaxErrorで失敗する。
メッセージは版により異なり、Python 3.11では`SyntaxError: expected '('`、
3.9・3.10では`SyntaxError: invalid syntax`となる。いずれも終了コード1で終わる。
指定した版をprek・pre-commitのいずれも解決できない場合は、フックの実行前に
`failed to find interpreter`で終了コード3となる。
prekは当該版を自動取得するため、この形になるのはuv管理のPythonも見つからない場合に限る。

ポイント。

- `uvx pyfltr fast`: uvがキャッシュするため2回目以降は実用速度で動作する
    - dev依存にpyfltrを加えている場合は`entry: uv run --frozen pyfltr fast`に置き換えてもよい
    - `uvx`が最新版を解決するのは初回だけで、以降はキャッシュ済みの版を再利用する。常に最新版を使う場合は`entry: uvx pyfltr@latest fast`と指定する
    - キャッシュが更新されて版が上がると、コードを変更していなくても新版のルール追加によりコミットが失敗しうる。版の変化に伴う失敗を避ける場合はdev依存へ固定し、上記の置き換えを採用する
    - `uvx`は`pyproject.toml`の`[tool.uv]`を読まないため、`exclude-newer`による公開直後版の回避はこの経路へ適用されない
- `fast`: mypy / pylint / pytestなど重いコマンドを除外した高速サブセット
    - formatterがファイルを修正しただけではフックを失敗と判定しない
- `types_or`: 必要な種別を列挙する
    - markdownはtextlint / markdownlint、TOML（pyproject.toml）でuv-sort、YAMLはactionlint
    - pre-commit・prekは`types_or`に一致するファイルがコミットに含まれない限りhook自体が起動されない。有効化したツールの対象種別が欠けていると、当該種別だけを変更したコミットで検査が実行されない
    - 有効化するツールを増やした場合は`types_or`の追随要否を確認する。`hadolint`（Dockerfile）・`shellcheck`・`shfmt`（シェル）などは`preset`に含まれないため、個別に有効化したときは対応する種別を追加する
- `require_serial: true`: pyfltr自身が内部で並列化するため、pre-commit側での多重起動を抑止する

pre-commit・prek統合の自動スキップなど双方向の挙動は[トラブルシューティング](troubleshooting.md)を参照。

## pyfltrとpre-commit・prekの呼び出し経路

pyfltrはpre-commit・prekのうち有効な方を内部で呼び出し、pre-commit・prekはpyfltrをフックとして呼び出す。
git commit経由でpre-commit・prekのいずれかが起動した場合、pyfltrは`PRE_COMMIT=1`を検出する。
pyfltrは内部の統合を自動スキップし、二重実行を防ぐ。

```mermaid
sequenceDiagram
    participant U as git commit
    participant PC as pre-commit / prek
    participant PH as pre-commit-hooks
    participant PF as pyfltr fast

    U->>PC: フック起動
    PC->>PH: check-yaml, trailing-whitespace等
    PC->>PF: pyfltr fast（local hook）
    Note over PF: PRE_COMMIT=1 検出で<br/>統合をスキップ
    PF->>PF: ruff-format, ruff-check等
```

逆に`make test`等から`pyfltr run`を呼び出した場合、pyfltr側が`SKIP=pyfltr`付きで有効なpre-commitまたはprekを起動する。
pre-commitとprekは、いずれも変更ファイル指定（`--files <対象>`）で起動する。
各hook内部の`types`・`types_or`・`files`・`exclude`フィルタはファイル指定起動でも適用されるため、関係するhookのみ動作する。
これによりpre-commit-hooks（check-yaml等）を統合実行できる。
詳細な挙動と無効化手順は[トラブルシューティング](troubleshooting.md)を参照。

## タスクランナー

pyfltrを呼び出すタスクランナーの設定例。
言語を問わず`uvx pyfltr`を利用できる。
pre-commit・prekはいずれもpyfltrの依存に含まれる。
`uvx pre-commit`・`uvx prek`で利用可能になる。
以降の例はprekを既定として示すが、`prek`関連コマンドを`pre-commit`へ置き換えても同様に動作する。
prekはworkspace rootからサブディレクトリの設定ファイルも再帰探索するため、
例では`--config=.pre-commit-config.yaml`で対象設定を固定している。
pre-commitへ置き換える場合は、この指定を削除する。

### Makefile

`uvx`方式ではuvがキャッシュするため2回目以降は実用速度で動作する。
最新版を解決するのは初回だけで、以降はキャッシュ済みの版を再利用する。
常に最新版を使う場合は`uvx pyfltr@latest ...`と指定する。

```makefile
.PHONY: format test

# フォーマット + 軽量lint（開発時の手動実行用。自動修正あり）
format:
	uvx pyfltr fast

# 全チェック実行（これを通過すればコミット可能）
test:
	uvx pyfltr run
```

dev依存にpyfltrを固定する運用では`UV_FROZEN`でlockfileを尊重しつつ、
`uv sync`後に`uv run pyfltr ...`を呼び出す形に置き換えることができる。

```makefile
export UV_FROZEN := 1

help:
	@cat Makefile

# 開発環境のセットアップ
setup:
	uv sync --all-groups --all-extras
	uvx prek --config=.pre-commit-config.yaml install

# 依存パッケージをアップグレードし全テスト実行
update:
	env --unset UV_FROZEN uv sync --upgrade --all-groups --all-extras
	uvx prek --config=.pre-commit-config.yaml autoupdate
	$(MAKE) test

# フォーマット + 軽量lint（開発時の手動実行用。自動修正あり）
format:
	uv run pyfltr fast

# 全チェック実行（これを通過すればコミット可能）
test:
	uv run pyfltr run

.PHONY: help setup update format test
```

`update`ターゲットが用いる`autoupdate`は、`prek --help`のコマンド一覧に現れないエイリアスである。
正規名は`update`で、どちらの名前を指定しても同じ動作をする（prek 0.4.11で確認）。
例が`autoupdate`を採用するのは、pre-commitから移行する際の書き換えが実行ファイル名の置換だけで済むためである。

### mise.toml

言語を問わず利用可能。

```toml
[tools]
# 既存のツール指定は残したまま追記する
uv = "latest"

[tasks.setup]
description = "開発環境のセットアップ"
run = [
  "...",
  "uvx prek --config=.pre-commit-config.yaml install",
]

[tasks.format]
description = "フォーマット + 軽量lint（開発時の手動実行用。自動修正あり）"
run = [
  "uvx pyfltr fast",
]

[tasks.test]
description = "全チェック（pyfltr runがprekまたはpre-commitを内部で呼び出す）"
run = [
  "uvx pyfltr run",
]

[tasks.ci]
description = "CI向け全チェック (差分検知で失敗)"
run = [
  "uvx pyfltr ci",
]
```

ポイント:

- `setup`: 開発環境のセットアップ
- `format`: `pyfltr fast`（fix段→formatter段→軽量linter段 + 内部prek・pre-commit統合）を実行する
    - `prek-fast`と`pre-commit-fast`が既定で`true`のため、有効化しているprekまたはpre-commitのhookもこの1コマンドで実行される
- `test`: ローカル開発用
    - `pyfltr run`が有効なprekまたはpre-commitを内部で呼び出すため、1コマンドで全チェックが完結する
- `ci`: CI用
    - `pyfltr ci`はformatter差分も含めて失敗扱いにする

## .markdownlint-cli2.yaml

markdownlint-cli2が読み込む設定ファイル。`$schema`を指定してエディタ補完を有効化する。

```yaml
$schema: https://raw.githubusercontent.com/DavidAnson/markdownlint-cli2/v0.20.0/schema/markdownlint-cli2-config-schema.json
config:
  # 行幅上限は設けず、1文1行運用（writing-standards準拠）で可読性を確保する
  line-length: false
  # コードブロック内でタブ文字を許可(Makefileなど用)
  no-hard-tabs:
    code_blocks: false
```

## .textlintrc.yaml

textlintで技術文書向けの複数プリセットと誤用語チェックを併用する例。
対応する`textlint-packages`の設定例は本ページ後半の「textlint-packagesのカスタマイズ」節を参照。

緩和対象は口語表現・比喩表現・冗長表現を検出するルールを除いたうえで、次のいずれかに当たるものとする。
自動修正が原文の表記を書き換え、修正の往復を生むもの。
他のルールまたは文書側の規範と同一の観点を重複して扱うもの。
専門用語の複合語や技術文の記述量で機械的な閾値超過が頻発するもの。
文書側で採る記法や自然な表現を誤って検出するもの。

```yaml
rules:
  preset-ja-technical-writing:
    # ラベル型見出し（"ポイント:", "例:" など）のため、文末句点の強制を無効化する
    ja-no-mixed-period: false
    # 技術文書における自然な助詞連結（「〜かどうかを検討するか」など）が頻出するため無効化する
    no-doubled-joshi: false
    # 全角丸括弧の閉じが改行をまたぐ書き方をfalse positiveとして誤検出するため無効化する
    no-unmatched-pair: false
    # 入れ子括弧の閉じ`））`連続をfalse positiveとして誤検出するため無効化する
    ja-no-successive-word: false
    # 引用文や詳細な技術説明で既定値超過が避けられないため緩和する。
    # 文長の抑制自体は冗長表現の抑止に寄与するため上限は残す
    sentence-length:
      max: 150
    # 専門用語の複合語で機械的な閾値超過が頻発し、回避に言い換えの手作業を要するため無効化する
    max-kanji-continuous-len: false
    # ドキュメントを常体（である調）で統一する方針のため
    no-mix-dearu-desumasu:
      preferInHeader: ""
      preferInBody: "である"
      preferInList: "である"
      strict: false
  preset-jtf-style:
    # no-mix-dearu-desumasuと同一の観点を重複検出するため無効化する
    "1.1.1.本文": false
    # 見出しの書き方は文書側の規範で扱うため無効化する
    "1.1.2.見出し": false
    "1.1.3.箇条書き":
      shouldUsePoint: false # 箇条書きは「。」をつけない
    # 和文の半角ピリオド・カンマ禁止ルール。`.gitignore`等のコード識別子の半角ピリオドを
    # 句点へ自動変換して破壊するため無効化する（lintは通過し検出できないため）
    "1.2.1.句点(。)と読点(、)": false
    "4.1.3.ピリオド(.)、カンマ(,)": false
    # 半角大かっこの全角自動変換がコード識別子・記法ラベルを破壊するため無効化する
    "4.3.2.大かっこ［］": false
    # 和文に隣接する半角丸かっこを全角へ自動変換し、表記の修正往復が頻発するため無効化する
    "4.3.1.丸かっこ（）": false
    # 改行折り返し時に全角括弧の前後スペースをfalse positiveとして誤検出するため無効化する
    "3.3.かっこ類と隣接する文字の間のスペースの有無": false
    # コロン終端のラベル記法を多用するため無効化する
    "4.2.7.コロン(：)": false
  ja-no-abusage: true
```

## textlint-packagesのカスタマイズ

追加のtextlintプリセットを使う場合は`textlint-packages`にパッケージ名を列挙する
（pnpx / npx起動時に`--package` / `-p`として展開される）。

```toml
[tool.pyfltr]
textlint-packages = [
    "textlint-rule-preset-ja-technical-writing",
    "textlint-rule-preset-jtf-style",
    "textlint-rule-ja-no-abusage",
]
```

共通のコマンドライン引数を追加したい場合は `textlint-args` を使う。
lint専用のオプション（`--format=compact` など）は `textlint-lint-args` に分離する。

```toml
[tool.pyfltr]
textlint-args = []
textlint-lint-args = ["--format", "compact"]
```

旧版の`textlint-args = ["--format", "compact", ...]`をそのまま引き継いでもクラッシュしない。
pyfltrはfix段の起動コマンドから`--format`ペアを自動除去するため。
ただし新規設定では`textlint-lint-args`に書くことを推奨する。

## 呼び出し方の使い分け {#calling-style}

状況に応じて`pyfltr`の呼び出し方を以下のいずれかから選ぶ。
コンテナ外では「キャッシュ済みの版を使う」（`uvx pyfltr ...`）、「常に最新版を使う」（`uvx pyfltr@latest ...`）、
「dev依存にバージョンを固定する」（`uv run pyfltr ...`）のいずれかをプロジェクト判断で選択する。

| 状況 | 呼び出し方 | 補足 |
| --- | --- | --- |
| 公式Dockerイメージ内（CI推奨構成） | `pyfltr ...` | イメージ同梱の本体を直接呼ぶ。uvキャッシュ経由の解決を経由しない |
| コンテナ外・キャッシュ済みの版を使う | `uvx pyfltr ...` | 初回のみ最新を解決し、以降はキャッシュを再利用する。ローカル開発で使用 |
| コンテナ外・常に最新版を使う | `uvx pyfltr@latest ...` | 毎回キャッシュをリフレッシュして最新を解決する。軽量CIで使用 |
| コンテナ外・dev依存に固定する | `uv run pyfltr ...` | `uv add --dev "pyfltr[python]"`済みのプロジェクトで使う。`UV_FROZEN`との併用が有効 |

## CI

GitHub Actionsでpyfltrを実行する構成の例。
リリース時に発行する公式Dockerイメージ`ghcr.io/ak110/pyfltr`を`container:`として利用する形が標準的。
`uv` / `pnpm` / `mise` / `hadolint` / `pinact` / `shellcheck`等が同梱されているため、
セットアップステップを毎回実行する必要がない。
キャッシュディレクトリは`/cache`配下にまとめて配置済み（`uv`は`/cache/uv`、`pnpm`は`/cache/pnpm`、`mise`は`/cache/mise`）。

Dockerイメージにpyfltr本体を同梱しているため、CI内では`pyfltr`を直接呼び出す。
`uvx pyfltr`を使うとコンテナ起動ごとにuvキャッシュ経由のツール解決が実行され、コンテナ同梱版を使う利点が薄れるため。

```yaml
jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.11", "3.12", "3.13", "3.14"]
    container:
      image: ghcr.io/ak110/pyfltr:latest
    defaults:
      run:
        shell: bash
    env:
      UV_PYTHON: ${{ matrix.python-version }}
    steps:
      - uses: actions/checkout@v7

      - name: Cache /cache
        uses: actions/cache@v6
        with:
          path: /cache
          key: pyfltr-cache-${{ runner.os }}-py${{ matrix.python-version }}-${{ github.run_id }}-${{ github.run_attempt }}
          restore-keys: pyfltr-cache-${{ runner.os }}-py${{ matrix.python-version }}-

      - name: Run pyfltr
        run: pyfltr ci --output-format=github-annotations
```

ポイント。

- `image: ghcr.io/ak110/pyfltr:latest`: `vX.Y.Z`タグも併発行されるため、再現性を重視する場合は固定タグを指定する
    - イメージには`UV_FROZEN=1`と`pnpm config set minimum-release-age 1440`が事前設定されているため、
    CIワークフロー側で同じ環境変数や設定を再指定する必要はない
- `defaults.run.shell: bash`: GitHub Actionsの`container:`既定シェルは`sh`であり、
  既存ワークフローで多用される`set -euo pipefail`等のbash前提の記述を通すために指定する
- `UV_PYTHON`: `uv run`が必要なCPythonをロックファイルとmatrix値に従って自動取得する
    - `actions/setup-python`は不要
    - 単一バージョンで十分な場合は`strategy.matrix`と`UV_PYTHON`を省ける
- `actions/cache`: `/cache`配下を一括キャッシュする
    - uv / pnpm / miseのキャッシュは内容アドレス指定のため、ロックファイル変更時も追加分がそのまま蓄積される
    - 固定キーは最初の保存後に更新されず、キャッシュの内容が初回保存時点のまま据え置かれる
    - キーへ`github.run_id`と`github.run_attempt`を併記し、`restore-keys`で直近のキャッシュへフォールバックする
    - `github.run_id`はワークフローの再実行（Re-run）で変化しないため、`github.run_id`だけでは
      再実行時にキーが完全一致し、`actions/cache`が新しい内容を保存しない。
      当該再実行中に変化した`/cache`の内容を後続の実行へ引き継ぐため、`github.run_attempt`を併記する
    - 復元は`tar -xf`による展開で、既存ファイルを保持するオプションを指定しない。
      コンテナーイメージ側で更新されたツールも、旧キャッシュに同名パスがあれば上書きされる。
      上記のキー構成は実行ごとに新しいキーを生成するだけであり、
      イメージ同梱ツールの更新をCIで検知する用途には応えない
    - 固定キーからこの構成へ移行する場合、`restore-keys`は前方一致で照合するため
      移行前の固定キー（末尾のハイフンを持たない）には一致しない。
      移行後、当該ブランチかつ同一`path`での初回実行だけがキャッシュ無しで実行される。
      移行前の固定キーを`restore-keys`の2行目へ併記すると当該の1回を避けられる。
      ただし効果は移行時の1回に限られる。
      当該prefixのキャッシュが保存された以降は1行目が一致するため、併記した行は使われない
    - 実行のたびに新しいエントリが増えるため、リポジトリのキャッシュ上限（既定10GB）と、
      7日間アクセスのないエントリが自動削除される仕様を前提に運用する
- `pyfltr ci`: イメージ同梱のpyfltrをそのまま使う
    - uvキャッシュを介した解決を毎回経由せず、コンテナビルド時に確定したバージョンで実行できる
    - 特定バージョンに固定したい場合は`image:`のタグ（`vX.Y.Z`）で揃える
- `--output-format=github-annotations`: `::error file=...` / `::warning file=...`形式の行を標準出力へ出力する
    - プル要求の該当ファイル行にコメントとして表示される

### 失敗時のログ保存（任意）

CIログのみでは失敗原因を切り分けられない場合（手元で再現しづらい環境固有の失敗等）に備え、
実行アーカイブを失敗時のジョブ成果物として保存する構成を追加できる。

````yaml
      - name: Run pyfltr
        env:
          PYFLTR_CACHE_DIR: /tmp/pyfltr-ci-archive
        run: pyfltr ci --output-format=github-annotations

      - name: 失敗時に実行アーカイブを保存
        if: failure()
        uses: actions/upload-artifact@v7
        with:
          name: pyfltr-archive-${{ runner.os }}-py${{ matrix.python-version }}
          path: ${{ env.PYFLTR_CACHE_DIR }}/runs
          retention-days: 7
````

`PYFLTR_CACHE_DIR`を明示するのは、既定の保存先（`platformdirs.user_cache_dir`）が
`container:`ジョブでは`$HOME`上書きの影響を受け不安定になり得るため。依存キャッシュ
（`/cache`配下）とは別パスを選び、アーカイブが依存キャッシュへ混入しないようにする。

### 追加のシステムパッケージが必要な場合

公式Dockerイメージの既定ユーザーは非root（sudo無し）のため、`apt`でシステムパッケージを追加するには
`container.options`で`--user root`を指定する。
ただし`--user root`実行時は、checkoutステップが用意したワークスペースの所有者と、コンテナ実行ユーザー（root）が一致しない。
この不一致により`pre-commit`等の`git`操作が`dubious ownership`で停止する。
`git config --global --add safe.directory`で対象ワークスペースを信頼対象に追加して回避する。
PDFを画像化する`pdf2image`が必要とする`poppler-utils`の導入と`safe.directory`設定を含めた例を次に示す。

```yaml
    container:
      image: ghcr.io/ak110/pyfltr:latest
      options: --user root
    steps:
      - uses: actions/checkout@v7
      - name: safe.directory の設定
        run: git config --global --add safe.directory "$GITHUB_WORKSPACE"
      - name: システムパッケージ導入
        run: |
          apt-get update
          apt-get install -y --no-install-recommends poppler-utils
```

### Dockerイメージを使わない場合（setup-uv方式）

自前runner制約等でDockerイメージを使用できない場合は、`astral-sh/setup-uv`でuvを導入し、Node.js / pnpmを別途セットアップする。
`UV_FROZEN`・`PYTHONDEVMODE`等の環境変数や`pnpm config set minimum-release-age 1440`は
ワークフロー側で個別指定が必要となる（Dockerイメージでは事前設定済み）。

```yaml
env:
  PYTHONDEVMODE: "1"
  UV_FROZEN: "1"

jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.11", "3.12", "3.13", "3.14"]
    steps:
      - uses: actions/checkout@v7
      - uses: astral-sh/setup-uv@v9
        with:
          python-version: ${{ matrix.python-version }}
          enable-cache: true
      - uses: actions/setup-node@v7
        with:
          node-version: "lts/*"
      - uses: pnpm/action-setup@v6
        with:
          version: latest
      - run: pnpm config set minimum-release-age 1440 --global
      - run: uvx pyfltr ci --output-format=github-annotations
      - run: uv cache prune --ci
```

`uv cache prune --ci`はプレビルドwheelをキャッシュから除去し、ソースビルドwheelのみ保持する。
GitHub Actionsのような高帯域環境ではキャッシュサイズを削減できる。
帯域が限られる環境では`--ci`を外し、プレビルドwheelもキャッシュに残す方が再取得を回避できる。

### PRの差分ファイルのみを対象にする

PR（プルリクエスト）で変更したファイルだけを対象に実行する場合は`--changed-since`を使う。
ベースブランチとの差分ファイルのみにチェック対象を限定し、大規模リポジトリでの実行時間を短縮できる。

```yaml
      - uses: actions/checkout@v7
        with:
          # 既定の単一コミット取得ではorigin/mainのリモート追跡refを生成しないため全履歴を取得する
          fetch-depth: 0

      - name: Test with pyfltr (changed files only)
        run: uvx pyfltr ci --changed-since=origin/main --output-format=github-annotations
```

`--changed-since=origin/main`は`origin/main`からの差分ファイルに限定して実行する。
対象は`git diff --name-only`が返すコミット差分・trackedファイルの作業ツリー差分・staged差分の和集合となり、
untrackedの新規ファイルは対象外。
gitが不在またはrefが解決できない場合は警告を出力して全体実行へフォールバックする。

`actions/checkout`の既定は`fetch-depth: 1`で、リモート追跡refはトリガーとなったref分だけを生成する。
pull_requestイベントでは`refs/remotes/pull/`配下だけを生成し`origin/main`が存在しないため、
`fetch-depth: 0`を指定しないと差分の基準refを解決できない。
この場合もジョブは失敗せず全体実行へフォールバックするため、短縮効果が失われた状態に気付きにくい。

### GitLab CIでMerge Requestへ表示する

GitLab CIでは`--output-format=code-quality`でCode Climate JSON issue形式のサブセットを出力する。
これを`artifacts:reports:codequality`としてアップロードするとMerge Request画面のCode Quality widgetに反映される。
MR diffインライン表示はUltimate tier限定。

```yaml
stages:
  - lint

pyfltr:
  stage: lint
  image: ghcr.io/astral-sh/uv:python3.13-bookworm
  variables:
    PYTHONDEVMODE: "1"
    UV_FROZEN: "1"
  script:
    - uvx pyfltr ci --output-format=code-quality --output-file=code-quality-report.json
  artifacts:
    when: always
    reports:
      codequality: code-quality-report.json
```

ポイント。

- `--output-format=code-quality`: Code Climate JSON issue形式の配列を出力する
    - `--output-file`を指定するとstdoutには従来の`text`整形出力が並行して出るため、ジョブログで進捗を確認できる
- `artifacts:reports:codequality`: 生成したJSONファイルをGitLabに取り込む
    - Merge Request画面のCode Quality widget（全tier）とMR diffインライン表示（Ultimate tier）に反映される
- `when: always`: ジョブが失敗してもアーティファクトを残す指定
    - lintエラーで`exit 1`したときもレポートを取り込めるようにする
- `image: ghcr.io/astral-sh/uv:python3.13-bookworm`: Node.js・pnpm・miseのいずれも含まないため、
  Python以外のツールの扱いに注意する
    - `textlint`・`markdownlint`は言語カテゴリゲートの対象外で、`preset`を指定すると有効になる
    - `js-runner`の既定値`pnpx`は論理設定値であり、PATH上の`pnpm`を使う`pnpm dlx`形式へ解決される
    - Markdownを含むリポジトリでは`pnpm`が見つからず、当該ツールは終了コード127の`failed`となる
    （解決失敗ではないため`{command}-severity = "warning"`で警告へ格下げできる）
    - 当該イメージはmiseも含まないため、既定で有効な`lychee`はbin-runnerでの解決に失敗し
    `resolution_failed`となる。解決失敗は`{command}-severity`による格下げの対象外のため、
    `markdownlint`・`textlint`を`false`にしてもジョブは`exit 1`のままとなる
    - `before_script`でNode.js・pnpm・miseを導入するか、`lychee`を含む該当ツールを`false`に設定する

---

Python以外のプロジェクトでの推奨設定例については[推奨設定例（非Pythonプロジェクト）](recommended-nonpython.md)を参照。
