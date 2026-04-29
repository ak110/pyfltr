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

ネイティブバイナリツール（cargo系・dotnet系を含む）は`pyfltr/command.py`の`_BIN_TOOL_SPEC`にmise backend付きで登録する。
あわせて`pyfltr/config.py`の`{command}-runner`既定値を`"bin-runner"`に揃える。
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
- `pyfltr.textout`: 人間向けテキスト出力。`pyfltr.cli.configure_text_output(stream, *, level)`で切り替える
- `pyfltr.structured`: JSONL / SARIF / Code Qualityの構造化出力。`pyfltr.cli.configure_structured_output(dest)`で切り替える

stdout占有は`jsonl` / `sarif` / `code-quality`かつ`--output-file`未指定時のみ発生する。

## subprocess起動時のPATH整理方針

pyfltrのCLI起動時に`os.environ["PATH"]`を順序先勝ちで重複排除して書き戻す。
これによりプロセス内で起動する全subprocessへ自動的に波及する。
書き換え位置は`pyfltr/main.py`の`main()`冒頭で、ライブラリ用途では実行されない。
重複排除の比較キーはOS依存に正規化する。
Windowsは大文字と小文字を区別せず、`/`と`\\`を等価に扱う。
POSIXは末尾スラッシュのみ落とす。
Windowsの`Path` / `PATH`揺れは検出したキー名のまま書き戻す。

mise経由のsubprocess（`bin-runner = "mise"`等で起動するもの）に限り、PATHから「miseが注入したtoolパス」を除外したenvを渡す。
対象パスは`mise/installs/`配下・`mise/dotnet-root`・`mise/shims`を含むエントリ。
判定ロジックは`pyfltr/command.py`の`_build_subprocess_env`が担う。
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
- グローバル設定の対象範囲・特殊仕様（archive/cache系のglobal優先）のSSOTは`pyfltr/config.py`の
  `ARCHIVE_CONFIG_KEYS` / `CACHE_CONFIG_KEYS` / `GLOBAL_PRIORITY_KEYS`定数。
  対象範囲を拡大する場合は実装・テスト・`docs/guide/configuration.md`を併せて更新する
- TOML読み書きは`tomlkit`に統一する（`tomllib`は使用しない）。
  `pyproject.toml`およびグローバル設定ファイル`config.toml`の読込・編集に適用する
- 本プロジェクトでは`CHANGELOG.md`を作成・維持しない。変更履歴はコミットメッセージとリリースタグで管理する
- mise backend既定値・tool spec組み立て仕様・`mise ls --current`結果に基づくtool spec省略判定のSSOTは
  `pyfltr/command.py`の`_BIN_TOOL_SPEC`および`build_commandline`・関連判定関数。
  変更時は`docs/guide/configuration-tools.md`・`docs/guide/recommended-nonpython.md`・`docs/guide/usage.md`の
  推奨設定例とコマンド表記を併せて更新する
- mise active tools取得結果の構造（`MiseActiveToolsResult`）とステータス語彙7値のSSOTは`pyfltr/command.py`。
  判定／JSONL header露出／`command-info`出力の3経路で同じ結果を共有する設計とする。
  ステータス追加や露出経路を増やすときは`docs/guide/usage.md`（command-info節・JSONLスキーマ節）と
  `docs/development/architecture.md`（mise active tools取得結果の構造化節）も併せて更新する
