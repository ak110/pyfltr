# CLAUDE.md: pyfltr

## 開発手順

- `make update`: 依存更新 + pre-commit autoupdate + pinactアクション更新 + 全テスト実行
  - `make update-actions`: GitHub Actionsのハッシュピン更新のみ（mise経由でpinact実行）
- リリース手順: [docs/development/development.md](docs/development/development.md) 参照
- テストコードは`pyfltr/xxx_.py`に対して`tests/xxx_test.py`として配置する
- コミット前の検証方法: `uv run --with-editable=. pyfltr run-for-agent`
  - ドキュメントなどのみの変更の場合は省略可（pre-commitで実行されるため）
  - テストコードの単体実行なども極力pyfltrを使う（pytestを直接呼び出さない）。
    具体的には `uv run --with-editable=. pyfltr run-for-agent <path>` 等
  - 修正後の再実行時は、対象ファイルや対象ツールを必要に応じて絞って実行する（最終検証はCIに委ねる前提）
    - 例: `uv run --with-editable=. pyfltr run-for-agent --commands=mypy,ruff-check path/to/file`

## 対応ツールの依存方針

pyfltrが対応するformatter/linter/testerの依存指定は以下の基準で振り分ける。

- 本体依存（`dependencies`）: 本家公式かつ自己完結なPyPIパッケージ
  - 汎用的に有用なもの（例: `typos`、`pre-commit`）
  - Python系ツール一式（例: `ruff`、`mypy`、`pylint`、`pyright`、`ty`、`pytest`、`uv-sort`）。
    `uvx pyfltr`単発で揃うようにし、`{command}-runner = "uv"`既定でcwdの`uv.lock`検出時は
    利用者プロジェクトの登録版へ切り替える。
    なお`pyright[nodejs]` extrasはインストール時にNode.jsランタイムを取得するため厳密には
   「自己完結」ではないが、Python系ツール一式を`uvx pyfltr`単発で揃える利便性を優先して同梱する
- 依存指定なし: 本家から独立した個人または別組織のメンテに依存するもの、
  インストール時に外部バイナリを取得するもの、Node.js等のランタイムを伴うもの。
  Node.js系・Goバイナリ・Rust/.NET系など利用者が個別に導入する

サードパーティの非公式PyPIラッパー（例: `shfmt-py`・`actionlint-py`・`shellcheck-py`）は、
本家から独立した個人または別組織のメンテに依存するため本体依存には組み込まない。
本家公式であってもインストール時に外部バイナリを取得するパッケージ（例: `editorconfig-checker`）は、
オフライン・プロキシ環境での導入失敗リスクを避けるため本体依存から除外する。
Node.js等のランタイムを伴うパッケージも、ランタイム導入とサプライチェーンの広さの観点から本体依存に含めない。

ネイティブバイナリツール（cargo系・dotnet系を含む）は`pyfltr/command/runner.py`の`_BIN_TOOL_SPEC`にmise backend付きで登録する。
あわせて`pyfltr/config/config.py`の`{command}-runner`既定値を`"bin-runner"`に揃える。
グローバル`bin-runner`既定値`"mise"`へ委譲することで、追加ツール導入時もmise経由の自動セットアップが既定動作となる。
利用者は`{command}-runner = "direct"`または`{command}-path`の明示で個別に切り戻せる。
新ツール追加時は本方針に従い、`_BIN_TOOL_SPEC`への登録と`{command}-runner` / `{command}-version`既定値の追加をセットで行う。

`[python]` extrasは空配列エイリアスとして残す。
本体依存にPython系ツール一式が同梱されているため、`uv add --dev "pyfltr[python]"`でも`uv add --dev pyfltr`でも利用者プロジェクトのvenvに同じものが入る。
extrasの空エイリアスは過去版からの利用者環境の`pyfltr[python]`指定をエラー化させない互換維持と、将来Python系ツールの依存配置を見直す際の表記予約のために維持する。
推奨表記はドキュメント側で`uv add --dev "pyfltr[python]"`を採用し、利用者から見える表記を将来の方針変更に対しても安定させる。

## ツール解決の優先順位

ツールsubprocess起動時は、`{command}-runner`設定値に応じて以下の優先順位で解決する。

- `{command}-path` 明示指定: 最優先で当該パスを採用する（Python系の既定値は空文字列で「未指定」扱い）
- `{command}-runner = "uv"`（Python系ツールの既定）:
  cwdに`uv.lock`があり、かつ`uv`バイナリが利用可能な場合は`uv run --frozen <bin>`経由でプロジェクトのvenvにあるツールを呼ぶ。
  いずれかが満たされない場合は`shutil.which`で本体依存に同梱されたバイナリを直接呼ぶ（directフォールバック）
- `{command}-runner = "mise"`: mise経由で解決
- `{command}-runner = "direct"`: PATH解決
- `{command}-runner = "js-runner"`: pnpx/npx経由で解決

cwdのuvプロジェクトに対象ツールが登録されていない場合、`uv run`側がエラーで失敗する。
利用者は当該ツールをプロジェクトに追加するか、`{command}-path`で明示するか、
`{command}-runner`を`"direct"`へ切り替えて対応する。

ツール解決経路の追跡情報はJSONL header（`uv_lock_present`・`uv_available`）と各commandレコード（`effective_runner`・`runner_source`）に出力する。
利用者・LLMが「想定どおりuv経路で動作したか」「direct fallbackが起きていないか」を出力から判別できるようにするための情報である。
スキーマ変更時は`docs/guide/usage.md`の「jsonlスキーマ」節と`mkdocs.yml`内llmstxtの該当節も併せて更新する。

## ドッグフーディング方針

pyfltr自身のリポジトリでは対応ツールを可能な限り有効化し、動作確認とサンプル設定の提示を兼ねる。
新ツールを追加した際は本プロジェクトでも合わせて有効化することを既定とし、毎回の判断は不要とする。

対象外とするのは次のいずれかに該当する場合のみ。

- 入力ファイルが本リポジトリに存在しないツール（例: `*.svelte`が無い状態でのsvelte-check）
- ネットワーク・認証等の外部依存によりCI安定度を著しく下げるツール（既定でdisable運用しているものを含む）

pyfltr自身の呼び出し方式は、利用者向け推奨（`uvx pyfltr ...`）を内部のツール解決機構で吸収する。
Python系ツール一式は本体依存に同梱されているため、利用者は`uvx pyfltr`単発で全機能を使える。
cwdに`uv.lock`がある場合は利用者プロジェクトのツール版が優先される。
ただし自リポでは「ローカル編集中のpyfltrを反映する」目的のため
`uv run --with-editable=. pyfltr ...`を採用する。
Makefile・pre-commitフック・GitHub Actions・GitLab CIのいずれも本方式で起動する。

## 出力形式とloggerの役割分担

pyfltrは3系統のloggerを使い分ける。
実装を変更する際はこの設計判断を崩さないこと。

format別のstream/level切替の詳細は[docs/development/architecture.md](docs/development/architecture.md#logger)を参照。

- root（system logger）: 常にstderr。抑止しない。設定エラー・アーカイブ初期化失敗などを流す
- `pyfltr.textout`: 人間向けテキスト出力。`pyfltr.cli.output_format.configure_text_output(stream, *, level)`で切り替える
- `pyfltr.structured`: JSONL / SARIF / Code Qualityの構造化出力。`pyfltr.cli.output_format.configure_structured_output(dest)`で切り替える

stdout占有は`jsonl` / `sarif` / `code-quality`かつ`--output-file`未指定時のみ発生する。

## モジュール構成方針

pyfltrのソースコードは`pyfltr/`直下に5つのサブパッケージ（`cli`・`command`・`config`・`output`・`state`）と
少数のトップレベルモジュール（`paths`・`warnings_`）で構成する。
サブパッケージごとの責務分離を維持し、命名は責務に沿わせる
（ガイダンス系は`cli`配下、実行系は`command`配下など）。
`__init__.py`ではre-exportせず、利用側はサブパッケージ内の具体モジュールから直接importする。
pyfltrはCLIツールであり、Pythonモジュールパスは内部実装として扱う。
内部リファクタリングではPython API互換性を維持しない。

サブパッケージ・モジュールごとの構成詳細とサブパッケージ間の依存方向は
[docs/development/architecture.md](docs/development/architecture.md#modules)を参照。

## subprocess起動時のPATH整理方針

pyfltrのCLI起動時に`os.environ["PATH"]`を順序先勝ちで重複排除して書き戻す。
これによりプロセス内で起動する全subprocessへ自動的に波及する。
書き換え位置は`pyfltr/cli/main.py`の`main()`冒頭で、ライブラリ用途では実行されない。
重複排除の比較キーはOS依存に正規化する。
Windowsは大文字と小文字を区別せず、`/`と`\\`を等価に扱う。
POSIXは末尾スラッシュのみ落とす。
Windowsの`Path` / `PATH`揺れは検出したキー名のまま書き戻す。

mise経由のsubprocess（`bin-runner = "mise"`等で起動するもの）に限り、PATHから「miseが注入したtoolパス」を除外したenvを渡す。
対象パスは`mise/installs/`配下・`mise/dotnet-root`・`mise/shims`を含むエントリ。
判定ロジックは`pyfltr/command/env.py`の`build_subprocess_env`が担う。
判定値は`ensure_mise_available`通過後の`ResolvedCommandline.effective_runner`を使う。
mise不在時のdirectフォールバック後の値で判断するため、`build_commandline`直後の値は採用しない。
`ensure_mise_available`内の`mise exec --version` / `mise trust`にも同じ除外envを明示的に渡す。
mise本体のバイナリディレクトリ（`mise/bin`を含むエントリ）は保護対象として除外しない。

本対応はmise側の挙動への対症療法である。
親PATHにmise自身のtoolエントリを見つけると、miseはtools解決をスキップしてPATH解決にフォールバックする。
mise側の修正後は本対応の撤去または維持を再検討する余地がある。

## 注意点

### SSOT・参照パス

- 内部リンクは英数アンカーを優先する。
  MkDocs（Material）のslugifyは英数のみを採用してアンカー生成するため、
  日本語アンカーリンク`#見出し日本語`はTOCで解決できずINFO通知のみで`--strict`でも検知されない（手動確認要）。
  markdownlint MD051は見出し原文を見るため、`{#id}`記法で明示併設する（例:「### jsonl形式の使い方 {#jsonl}」）
- `docs/guide/index.md`の対応ツール一覧と`mkdocs.yml`内llmstxt `markdown_description`の「対応ツール」節は人手同期（SSOT化しない運用）
- ty記述のSSOTは`docs/guide/index.md`。
  preset非収録の扱いを変更した場合は`README.md`・`mkdocs.yml`内llmstxt・`docs/guide/configuration.md`・`docs/guide/usage.md`を併せて更新する
- サブコマンド一覧のSSOTは`docs/guide/usage.md`。
  サブコマンドを追加・削除した場合は`README.md`の「主なサブコマンド」節と`mkdocs.yml`内llmstxtの「サブコマンド」節を併せて更新する
- `mkdocs.yml`内llmstxt `markdown_description`にはLLMが利用する際に有用な情報のみ記載する（`run-for-agent`サブコマンド・主要オプションなど）。
  LLMにとって不要な情報はdocs側をSSOTとし、多重管理を避ける
- 出力形式解決のSSOTは`docs/guide/usage.md`「出力形式の切り替え」節。
  優先順位は`CLI > PYFLTR_OUTPUT_FORMAT > サブコマンド既定値 > AI_AGENT(jsonl) > text`、サブコマンド別許容値は実行系5値・参照系3値・`command-info`2値。
  解決ロジック本体は`pyfltr/cli/output_format.py`の`resolve_output_format`に置く。
  挙動変更時は実装・`docs/guide/usage.md`・`mkdocs.yml`内llmstxtを併せて更新する
- グローバル設定の対象範囲・特殊仕様（archive/cache系のglobal優先）のSSOTは
  `pyfltr/config/config.py`の`ARCHIVE_CONFIG_KEYS` / `CACHE_CONFIG_KEYS` / `GLOBAL_PRIORITY_KEYS`定数。
  対象範囲を拡大する場合は実装・テスト・`docs/guide/configuration.md`を併せて更新する
- mise backend既定値・tool spec組み立て仕様・`mise ls --current`結果に基づくtool spec省略判定のSSOTは
  `pyfltr/command/runner.py`の`_BIN_TOOL_SPEC`および`build_commandline`・関連判定関数。
  変更時は`docs/guide/configuration-tools.md`・`docs/guide/recommended-nonpython.md`・`docs/guide/usage.md`の
  推奨設定例とコマンド表記を併せて更新する
- mise active tools取得結果の構造（`MiseActiveToolsResult`）とステータス語彙7値のSSOTは`pyfltr/command/mise.py`。
  判定／JSONL header露出／`command-info`出力の3経路で同じ結果を共有する設計とする。
  ステータス追加や露出経路を増やすときは`docs/guide/usage.md`（command-info節・JSONLスキーマ節）と
  `docs/development/architecture.md`（mise active tools取得結果の構造化節）も併せて更新する
- モジュールパス参照を含むドキュメントはモジュール移動の際に追従更新が必要。
  主な対象は`CLAUDE.md`（本ファイル）と`docs/development/architecture.md`
- 本プロジェクトでは`CHANGELOG.md`を作成・維持しない。変更履歴はコミットメッセージとリリースタグで管理する

### LLM出力スキーマ

- JSONLスキーマの変更は破壊的変更扱いしない（LLMが読みやすいよう継続的に改善する）
- JSONL出力フィールドは自己説明性を優先し、フィールド意味の補足説明はドキュメント側
 （`docs/guide/usage.md`の「jsonlスキーマ」節）に集約する
- JSONLレコード（`header` / `diagnostic` / `command` / `warning` / `summary`）のスキーマ変更時は、
  `docs/guide/usage.md`の「jsonlスキーマ」節を更新する。
  `mkdocs.yml`内llmstxtにはJSONLスキーマの詳細を持たせず`docs/guide/usage.md`への参照リンクで委譲する方針のため、
  llmstxt側の更新は不要
- JSONL出力の`command.hints`は「対応する指摘やステータスが実際に該当するときのみ付与する」方針とする。
  指摘0件の実行で固定的なhintが残るとLLM入力のトークンを浪費するため、
  `aggregate_diagnostics`由来のhintは指摘ある時のみ集約し、
  ツール固有のhint（`messages[].col`等）も付与条件に当該指摘・状態の存在を含める
- 複数の関連フィールド（例:`messages[].col`と`messages[].end_col`）に同じ説明文が及ぶ場合は、
  代表キー1つに統合した1つのhintで両方をまとめて説明する。
  類似文言の重複はLLM入力のトークンを浪費するため、付与時はキーを1個・文言を1個に集約する
- `summary.commands_summary`統計は`failed`等の判定に必要な項目を常時出力し、
  `resolution_failed`のような付加情報のみ0件で省略する
- 個別ルールの`command.hints`とパイプライン全体の`summary.guidance`は粒度・性質が異なるため命名を分ける
- `command.hints`・`summary.guidance`はLLM入力前提のため英語で記述する。
  トークン効率と汎用性を優先し、「全文章は日本語」方針より優先する例外として扱う

### テスト・実装制約

- TOML読み書きは`tomlkit`に統一する（`tomllib`は使用しない）。
  `pyproject.toml`およびグローバル設定ファイル`config.toml`の読込・編集に適用する
- 実行内（プロセス全体で1回計算したい）キャッシュは`@functools.lru_cache(maxsize=1)`で実装する。
  モジュール変数＋`global`文の代替案よりpylint抑止が不要で、関数として参照できるため`monkeypatch.setattr`でテスト差し替えできる利点がある
- 関数内ローカルimportは「循環import発生時のみ」「オプショナル依存のtry/except内」の2用途に限定する。
  起動時間の最適化を目的とした遅延importは行わない（測定根拠が無い限り早期最適化に該当するため）。
  動的フォーマッター登録のような構造的事情は、レジストリ初期化を呼び出し側へ集約することで遅延importを回避する
- 同一サブパッケージ内のモジュール間importは、`pyright`が関数内ローカルimportを未解決として誤検知する事象がある。
  特に`pyfltr/command/dispatcher.py`は他のcommand配下モジュールを参照する都合で関数内ローカルimportを避けてモジュールレベルimportで統一する。
  循環import発生時のみローカルimportに切り替える方針を取る
- インライン抑止コメント（`# pylint: disable=`・`# noqa`・`# type: ignore`等）は、
  ルール本来の意図が当該箇所に当てはまらない例外を局所的に示す目的に限定する。
  構造的問題（重複ロジック・循環依存・private属性参照の常態化など）の回避手段として使わない。
  やむを得ず残す場合は同一行または直前行に理由コメントを併記する。
  同一抑止が複数箇所で必要になる場合は、設定ファイル側での扱い（per-file-ignore追加等）をユーザーと相談する
- pre-commit hookの`entry:`で`uv run`を起動する場合、必ず`--frozen`を明示する。
  pre-commitは親の環境変数を引き継がない構成のため`UV_FROZEN`が未設定で来る可能性があり、
  明示しないとlockfile更新が走り得る。
  Makefileは`export UV_FROZEN := 1`で覆っているが、pre-commit hookはその恩恵を受けない
- pyfltrテストでは`AI_AGENT` / `PYFLTR_OUTPUT_FORMAT`が予期せず設定されているとjsonl既定へ切り替わる。
  テスト挙動が揺らがないよう、
  `tests/conftest.py`のautouseフィクスチャ`_isolate_output_format_envs`で両環境変数を未設定にする。
  値を設定するテストでのみ`monkeypatch.setenv`で個別に上書きする
- テストで`pyfltr.config.config.load_config()`経由の設定値を差し替えたい場合は、
  `monkeypatch.setattr(pyfltr.config.config, "load_config", lambda **_kw: <test_config>)`の形で
  関数自体を置換する。
  autouseフィクスチャ`_isolate_global_config`は環境変数`PYFLTR_GLOBAL_CONFIG`をtmpパスへ固定するのみで、
  cwdの`pyproject.toml`は依然として読み込まれる。
  pyproject.tomlの値に左右されないテストとしたい場合は`load_config`自体の差し替えが必要
- テストコードからの実装参照には2系統があり、リファクタリング時は両方を漏れなく追従させる。
  `import`文・`from ... import ...`は静的解析で検出できるが、
  `monkeypatch.setattr("pyfltr.command.xxx....")` / `mocker.patch("pyfltr.command.xxx....")` /
  `caplog`等のlogger名指定の文字列引数は静的解析で検出できない。
  サブパッケージ移動・リネームのたびに`grep -rn 'pyfltr\.<旧パス>'`で全文検索して網羅置換する
- テストコードから`pyfltr.command.runner`内の`shutil.which`をmockする場合、
  グローバルな`shutil.which`単独パッチでは効かないため、
  `monkeypatch.setattr("pyfltr.command.runner.shutil.which", ...)`のように
  モジュールパス単位でターゲットを明示する
