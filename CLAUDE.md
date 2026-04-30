# CLAUDE.md: pyfltr

## 開発手順

- `make update`: 依存更新 + pre-commit autoupdate + pinactアクション更新 + 全テスト実行
  - `make update-actions`: GitHub Actionsのハッシュピン更新のみ（mise経由でpinact実行）
- リリース手順: [docs/development/development.md](docs/development/development.md) 参照
- テストコードは`pyfltr/xxx_.py`に対して`tests/xxx_test.py`として配置する
- コミット前の検証方法: `uv run pyfltr run-for-agent`
  - ドキュメントなどのみの変更の場合は省略可（pre-commitで実行されるため）
  - テストコードの単体実行なども極力 `uv run pyfltr run-for-agent <path>` を使う（pytestを直接呼び出さない）
    - 詳細な情報などが必要な場合に限り `uv run pytest -vv <path>` などを使用
  - 修正後の再実行時は、対象ファイルや対象ツールを必要に応じて絞って実行する（最終検証はCIに委ねる前提）
    - 例: `pyfltr run-for-agent --commands=mypy,ruff-check path/to/file`

## 対応ツールの依存方針

pyfltrが対応するformatter/linter/testerの依存指定は以下の基準で振り分ける。
ここでの「公式」は対象ツール本家プロジェクトが直接配布しているPyPIパッケージを指す
（GitHubの本家organization配下から配布されているものを含む）。
さらに「自己完結」とは、wheelにバイナリ等が同梱されており`pip install`時に外部ネットワーク取得を発生させないことを指す。

- 本体依存（`dependencies`）: 本家公式かつ自己完結なPyPIパッケージで、
  Pythonプロジェクト以外でも汎用的に有用なもの（例: `typos`、`pre-commit`）
- python extras（`[python]`）: 本家公式のPyPIパッケージだがPythonプロジェクト専用のもの
 （例: `ruff`、`mypy`、`pylint`、`pyright`、`ty`、`pytest`、`uv-sort`）
- 依存指定なし: 上記いずれにも該当しないもの。
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

## ドッグフーディング方針

pyfltr自身のリポジトリでは対応ツールを可能な限り有効化し、動作確認とサンプル設定の提示を兼ねる。
新ツールを追加した際は本プロジェクトでも合わせて有効化することを既定とし、毎回の判断は不要とする。

対象外とするのは次のいずれかに該当する場合のみ。

- 入力ファイルが本リポジトリに存在しないツール（例: `*.svelte`が無い状態でのsvelte-check）
- ネットワーク・認証等の外部依存によりCI安定度を著しく下げるツール（既定でdisable運用しているものを含む）

## 出力形式とloggerの役割分担

pyfltrは3系統のloggerを使い分ける。
実装を変更する際はこの設計判断を崩さないこと。

format別のstream/level切替の詳細は[docs/development/architecture.md](docs/development/architecture.md#logger)を参照。

- root（system logger）: 常にstderr。抑止しない。設定エラー・アーカイブ初期化失敗などを流す
- `pyfltr.textout`: 人間向けテキスト出力。`pyfltr.cli.output_format.configure_text_output(stream, *, level)`で切り替える
- `pyfltr.structured`: JSONL / SARIF / Code Qualityの構造化出力。`pyfltr.cli.output_format.configure_structured_output(dest)`で切り替える

stdout占有は`jsonl` / `sarif` / `code-quality`かつ`--output-file`未指定時のみ発生する。

## モジュール構成

pyfltrのソースコードは`pyfltr/`直下に5つのサブパッケージと少数のトップレベルモジュールで構成される。

### サブパッケージ

- `pyfltr/cli/`: CLIエントリポイントと各サブコマンドのハンドラー
  - `cli/main.py`: `main()` / `run()`。エントリポイントとサブコマンドdispatch
  - `cli/parser.py`: `build_parser()` / `make_common_parent()`。argparse構築
  - `cli/pipeline.py`: `run_impl()` / `run_pipeline()` / `run_commands_with_cli()`。パイプライン本体
  - `cli/render.py`: `render_results()` / `write_log()`。text整形描画。`output/formatters.py`から呼ばれる
  - `cli/output_format.py`: `resolve_output_format()` / `text_logger` / `structured_logger`。出力形式解決とログ設定
  - `cli/command_info.py`: `command-info`サブコマンド
  - `cli/config_subcmd.py`: `config`サブコマンド
  - `cli/shell_completion.py`: `generate-shell-completion`サブコマンド
  - `cli/mcp_server.py`: `mcp`サブコマンド（MCPサーバー）
  - `cli/precommit_guidance.py`: pre-commit統合ガイダンス
- `pyfltr/command/`: コマンド実行コア
  - `command/core_.py`: `ExecutionBaseContext` / `ExecutionContext` / `CommandResult` / `CacheContext` / `ExecutionParams`。実行コンテキスト型
  - `command/process.py`: `ProcessRegistry` / `run_subprocess` / `terminate_active_processes`。プロセス管理
  - `command/mise.py`: `MiseActiveToolsResult` / `get_mise_active_tools`。mise統合
  - `command/env.py`: `dedupe_environ_path` / `build_subprocess_env`。subprocess環境構築
  - `command/runner.py`: `_BIN_TOOL_SPEC` / `build_commandline` / `ensure_mise_available` / `build_invocation_argv`。runner解決とコマンドライン構築
  - `command/targets.py`: `expand_all_files` / `filter_by_globs` / `filter_by_changed_since` / `excluded`。対象ファイル選定
  - `command/snapshot.py`: `snapshot_file_digests` / `changed_files`。ファイル変更検知
  - `command/dispatcher.py`: `execute_command`。ディスパッチャー
  - `command/glab.py`: `execute_glab_ci_lint`。glab関連
  - `command/precommit.py`: `execute_pre_commit`。pre-commit実行
  - `command/linter_fix.py`: `execute_linter_fix`。fixモードでのlinter実行
  - `command/textlint_fix.py`: `execute_textlint_fix`。textlintのfixモード実行
  - `command/builtin.py`: `BUILTIN_COMMANDS` / `CommandInfo`。ビルトインコマンド定義
  - `command/error_parser.py`: `ErrorLocation` / `parse_errors`。エラーパーサー
  - `command/two_step/base.py`: `execute_ruff_format_two_step` / `execute_check_write_two_step` /
    `execute_prettier_two_step` / 各種共通基底ヘルパー。段階制御パイプラインの本体
  - `command/two_step/ruff.py`: `execute_ruff_format_two_step`のエイリアス（base.pyへ委譲）
  - `command/two_step/taplo.py`: `execute_taplo_two_step`のエイリアス（base.pyへ委譲）
  - `command/two_step/shfmt.py`: `execute_shfmt_two_step`のエイリアス（base.pyへ委譲）
  - `command/two_step/prettier.py`: `execute_prettier_two_step`のエイリアス（base.pyへ委譲）
- `pyfltr/config/`: 設定読み書きとプリセット
  - `config/config.py`: `Config` / `load_config()`等。設定の読み書き・解決。
    `BUILTIN_COMMANDS`等のビルトインツール定義は`command/builtin.py`が実体だが、
    `config/config.py`内のロジックでも参照するため`from pyfltr.command.builtin import ...`で取り込みつつ、
    `pyfltr.config.config.BUILTIN_COMMANDS`として参照する利用側コード（`cli/parser.py`・テスト群）の
    便宜のため`__all__`にも含めて再エクスポート扱いとする
  - `config/presets.py`: プリセット定義
- `pyfltr/output/`: 出力フォーマット群
  - `output/formatters.py`: `RunOutputContext` / `FORMATTERS`レジストリ。フォーマット基盤
  - `output/jsonl.py`: JSONL出力（`--output-format=jsonl`）
  - `output/sarif.py`: SARIF出力（`--output-format=sarif`）
  - `output/code_quality.py`: GitLab Code Quality出力
  - `output/github_annotations.py`: GitHub Annotations出力
  - `output/ui.py`: Textual UI（`--ui`）
  - `output/rule_urls.py`: ツール別ルールURL生成
- `pyfltr/state/`: アーカイブ・キャッシュ・履歴・再実行制御の永続化系
  - `state/archive.py`: 実行アーカイブ読み書き
  - `state/cache.py`: ファイルhashキャッシュ
  - `state/runs.py`: `list-runs` / `show-run`サブコマンド
  - `state/only_failed.py`: `--only-failed`フィルター処理
  - `state/retry.py`: `retry_command`生成
  - `state/executor.py`: コマンド実行順制御
  - `state/stage_runner.py`: ステージ実行ヘルパー

### 設計方針

- 命名は責務に沿う。ガイダンス系（`precommit_guidance`等）は`cli`配下、実行系は`command`配下
- `__init__.py`ではre-exportせず、利用側はサブパッケージ内の具体モジュールから直接importする
- pyfltrはCLIツールであり、Pythonモジュールパスは内部実装として扱う。
  内部リファクタリングではPython API互換性を維持しない

### トップレベルモジュール

- `pyfltr/paths.py`: パスユーティリティ
- `pyfltr/warnings_.py`: 警告蓄積

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

- 内部リンクは英数アンカーを優先する。MkDocs（Material）のslugifyは英数のみを採用してアンカー生成するため、
  日本語アンカーリンク`#見出し日本語`はTOCで解決できずINFO通知のみで`--strict`でも検知されない（手動確認要）。
  markdownlint MD051は見出し原文を見るため、`{#id}`記法で明示併設する（例:「### jsonl形式の使い方 {#jsonl}」）
- `docs/guide/index.md`の対応ツール一覧と`mkdocs.yml`内llmstxt `markdown_description`の「対応ツール」節は
  人手同期（SSOT化しない運用）
- ty記述のSSOTは`docs/guide/index.md`。preset非収録の扱いを変更した場合は
  `README.md`・`mkdocs.yml`内llmstxt・`docs/guide/configuration.md`・`docs/guide/usage.md`を併せて更新する
- サブコマンド一覧のSSOTは`docs/guide/usage.md`。サブコマンドを追加・削除した場合は
  `README.md`の「主なサブコマンド」節と`mkdocs.yml`内llmstxtの「サブコマンド」節を併せて更新する
- `mkdocs.yml`内llmstxt `markdown_description`にはLLMが利用する際に有用な情報のみ記載する
 （`run-for-agent`サブコマンド、主要オプションなど）。LLMにとって不要な情報はdocs側をSSOTとし、多重管理を避ける
- JSONLスキーマの変更は破壊的変更扱いしない（LLMが読みやすいよう継続的に改善する）
- JSONL出力フィールドは自己説明性を優先し、フィールド意味の補足説明はドキュメント側
 （`docs/guide/usage.md`の「jsonlスキーマ」節）に集約する
- JSONL出力の`command.hints`は「対応する指摘やステータスが実際に該当するときのみ付与する」方針とする。
  指摘0件の実行で固定的なhintが残るとLLM入力のトークンを浪費するため、
  `aggregate_diagnostics`由来のhintは指摘ある時のみ集約され、
  ツール固有のhint（`messages[].end_col`等）も付与条件に当該指摘・状態の存在を含める
- `summary.commands_summary`統計は`failed`等の判定に必要な項目を常時出力し、
  `resolution_failed`のような付加情報のみ0件で省略する
- 個別ルールの`command.hints`とパイプライン全体の`summary.guidance`は粒度・性質が異なるため命名を分ける
- `command.hints`・`summary.guidance`はLLM入力前提のため英語で記述する。
  トークン効率と汎用性を優先し、「全文章は日本語」方針より優先する例外として扱う
- JSONLレコード（`header` / `diagnostic` / `command` / `warning` / `summary`）のスキーマ変更時は、
  `mkdocs.yml`内llmstxt `markdown_description`の該当節も併せて更新する
- グローバル設定の対象範囲・特殊仕様（archive/cache系のglobal優先）のSSOTは`pyfltr/config/config.py`の
  `ARCHIVE_CONFIG_KEYS` / `CACHE_CONFIG_KEYS` / `GLOBAL_PRIORITY_KEYS`定数。
  対象範囲を拡大する場合は実装・テスト・`docs/guide/configuration.md`を併せて更新する
- TOML読み書きは`tomlkit`に統一する（`tomllib`は使用しない）。
  `pyproject.toml`およびグローバル設定ファイル`config.toml`の読込・編集に適用する
- 本プロジェクトでは`CHANGELOG.md`を作成・維持しない。変更履歴はコミットメッセージとリリースタグで管理する
- mise backend既定値・tool spec組み立て仕様・`mise ls --current`結果に基づくtool spec省略判定のSSOTは
  `pyfltr/command/runner.py`の`_BIN_TOOL_SPEC`および`build_commandline`・関連判定関数。
  変更時は`docs/guide/configuration-tools.md`・`docs/guide/recommended-nonpython.md`・`docs/guide/usage.md`の
  推奨設定例とコマンド表記を併せて更新する
- mise active tools取得結果の構造（`MiseActiveToolsResult`）とステータス語彙7値のSSOTは`pyfltr/command/mise.py`。
  判定／JSONL header露出／`command-info`出力の3経路で同じ結果を共有する設計とする。
  ステータス追加や露出経路を増やすときは`docs/guide/usage.md`（command-info節・JSONLスキーマ節）と
  `docs/development/architecture.md`（mise active tools取得結果の構造化節）も併せて更新する
- 出力形式解決のSSOTは`docs/guide/usage.md`「出力形式の切り替え」節。
  優先順位は`CLI > PYFLTR_OUTPUT_FORMAT > サブコマンド既定値 > AI_AGENT(jsonl) > text`、
  サブコマンド別許容値は実行系5値・参照系3値・`command-info`2値。
  解決ロジック本体は`pyfltr/cli/output_format.py`の`resolve_output_format`に集約している。
  挙動変更時は実装・`docs/guide/usage.md`・`mkdocs.yml`内llmstxtを併せて更新する
- pyfltrテストでは`AI_AGENT` / `PYFLTR_OUTPUT_FORMAT`が予期せず設定されているとjsonl既定へ切り替わる。
  テスト挙動が揺らがないよう、`tests/conftest.py`のautouseフィクスチャ`_isolate_output_format_envs`で
  両環境変数を未設定化済み。値を設定するテストでのみ`monkeypatch.setenv`で個別に上書きする
- モジュールパス参照を含むドキュメントはモジュール移動の際に追従更新が必要。
  主な対象: `CLAUDE.md`（本ファイル）、`docs/development/architecture.md`
- テストコードからの実装参照には2系統があり、リファクタリング時は両方を漏れなく追従させる。
  `import`文・`from ... import ...`は静的解析で検出できるが、
  `monkeypatch.setattr("pyfltr.command.xxx....")` / `mocker.patch("pyfltr.command.xxx....")` /
  `caplog`等のlogger名指定の文字列引数は静的解析で検出できない。
  サブパッケージ移動・リネームのたびに`grep -rn 'pyfltr\.<旧パス>'`で全文検索して網羅置換する
- 同一サブパッケージ内のモジュール間importは、`pyright`が関数内ローカルimportを未解決として誤検知する事象がある。
  特に`pyfltr/command/dispatcher.py`は他のcommand配下モジュールを参照する都合で関数内ローカルimportを
  避けてモジュールレベルimportで統一している。
  循環import発生時のみローカルimportに切り替える方針を取る
- 関数内ローカルimportは「循環import発生時のみ」「オプショナル依存のtry/except内」の2用途に限定する。
  起動時間の最適化を目的とした遅延importは行わない（測定根拠が無い限り早期最適化に該当するため）。
  動的フォーマッター登録のような構造的事情は、レジストリ初期化を呼び出し側へ集約することで遅延importを回避する
- インライン抑止コメント（`# pylint: disable=`、`# noqa`、`# type: ignore`等）は、ルール本来の意図が当該箇所に当てはまらない例外を局所的に示す目的に限定する。
  構造的問題（重複ロジック・循環依存・private属性参照の常態化など）の回避手段として使わない。
  やむを得ず残す場合は同一行または直前行に理由コメントを併記する。
  同一抑止が複数箇所で必要になる場合は、設定ファイル側での扱い（per-file-ignore追加等）をユーザーと相談する
