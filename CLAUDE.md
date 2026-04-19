# CLAUDE.md: pyfltr

## 開発手順

- `make update`: 依存更新 + pre-commit autoupdate + pinactアクション更新 + 全テスト実行
  - `make update-actions`: GitHub Actionsのハッシュピン更新のみ（mise経由でpinact実行）
- テストコードは`pyfltr/xxx_.py`に対して`tests/xxx_test.py`として配置する
- 実行パイプラインの構造: `run_pipeline`（main.py）がTUI/非TUI分岐の最上位関数。
  パイプライン共通の前処理（ファイル展開など）はこの関数内でTUI起動前に実行する
- コミット前の検証方法: `uv run pyfltr run-for-agent`
  - ドキュメントなどのみの変更の場合は省略可（pre-commitで実行されるため）
  - テストコードの単体実行なども極力 `uv run pyfltr run-for-agent <path>` を使う（pytestを直接呼び出さない）
    - 詳細な情報などが必要な場合に限り `uv run pytest -vv <path>` などを使用
  - JSONL出力は`header`（実行環境・`run_id`・`schema_hints`）→ `diagnostic`+`tool`（ツール完了ごと）→
    `warning`→ `summary`（末尾）の順に出力される。末尾の`summary`で`failed`と`diagnostics`を確認し、
    必要に応じて`diagnostic`行の`messages[]`内のファイル・行番号・メッセージを参照する。
    `header.run_id`（ULID）は実行アーカイブの参照キー
  - `header.schema_hints`はJSONL各フィールドの意味をLLM向け英語ガイドとして毎runに付与する
  - `summary.guidance`（英語配列）は`failed > 0`のときのみ付与され、
    `tool.retry_command` / `--only-failed` / `show-run` の案内を示す
  - `summary.fully_excluded_files`（任意）は、直接指定されたが`exclude`パターンや`.gitignore`で全除外されたファイル一覧。
    非空時のみ付与し、exitコードは0のままだが「警告0件＝問題なし」と誤解しないよう明示する
  - `diagnostic`は`(tool, file)`単位で1行に集約される。個別の指摘は`messages[]`配列に並び、
    `(line, col or 0, rule or "")`の昇順でソートされている
  - `messages[]`要素の任意フィールド: `col`・`rule`・`severity`（3値に正規化）・`fix`（値域の詳細は後述）・`hint`。`msg`は必須
  - `fix`は`"safe"` / `"unsafe"` / `"suggested"` / `"none"`の4値。ツールが自動修正情報を返さない場合はフィールドごと省略
  - `hint`はルール単位の短い修正ヒント。pyfltr側で事前登録したルールのみに付与される。
    対象はtextlintの頻出ルール（`sentence-length` / `max-ten` / `max-kanji-continuous-len`）など
  - ルールURLは`tool`レコード末尾の`hint-urls`辞書（ハイフン区切りキー・rule→URL）にツール単位で集約される。
    URLを生成できたruleのみ含む
  - `tool`の任意フィールド: `retry_command`（当該ツールで失敗したファイルのみに絞った再実行コマンド）・
    `truncated`（smart truncation発生時。`archive`パスで全文参照）・`hint-urls`（rule→URL辞書）
  - `retry_command`の追加仕様: 失敗時（`has_error=true`）のみ出力。成功時・`cached=true`時は出力されない。
    失敗ファイルを特定できない場合はターゲット位置引数が空
  - `tool`のキャッシュ関連フィールド: `cached`（ファイルhashキャッシュから復元されたとき `true`）・
    `cached_from`（`cached=true` 時のソース `run_id`）
  - 詳細仕様は`docs/guide/usage.md`の「jsonlスキーマ」節および`llms.txt`を参照。
    `--output-format=sarif` / `github-annotations` でCI向け形式にも切り替え可能
  - `--commands=<tool>`: 個別ツールだけを実行する（例: `pyfltr run --commands=textlint docs/`）。
    ツール名やエイリアス（`format` / `lint` / `test`）をサブコマンドとして直接渡すことはできない
   （誤入力時は実行例付きのエラーメッセージが出る）
  - `--fail-fast`: 1ツールでもエラーが出た時点で残りを打ち切る（起動済みはterminate、未開始はskipped扱い）
  - `--no-cache`: ファイルhashキャッシュを無効化する。現状はtextlintのみ対象
  - `--only-failed`: 直前runから失敗ツール・失敗ファイルを抽出し、ツール別にその組み合わせのみ再実行する。
    直前run無し/失敗ツール無し/targets交差空ならメッセージを出してrc=0で成功終了する
  - `--from-run <RUN_ID>`: `--only-failed`の参照対象runを明示指定する（前方一致・`latest`対応）。
    併用前提で単独指定は拒否。不在`<RUN_ID>`時は警告を出してrc=0で早期終了する
  - `header.run_id`はユーザーキャッシュに保存された該当runの参照キー。`pyfltr list-runs`で一覧、
    `pyfltr show-run <run_id>`で詳細（`<run_id>`は前方一致・`latest`エイリアス可）を参照する。
    `--tool <name>`でdiagnostics全件、`--tool <name> --output`で`output.log`全文が得られる
  - MCPクライアント（Claude Desktopなど）からは`pyfltr mcp`でMCPサーバーを起動する。
    提供ツールは`list_runs` / `show_run` / `show_run_diagnostics` / `show_run_output` / `run_for_agent`の5種類で、
    アーカイブ参照と実行を行える

## 出力形式とloggerの役割分担

pyfltrは3系統のloggerを使い分ける。実装を変更する際はこの設計判断を崩さないこと。

- root（system logger）: 常にstderr。抑止しない。設定エラー・アーカイブ初期化失敗などを流す
- `pyfltr.textout`: 人間向けテキスト出力（進捗・`write_log`・summary・warnings・`--only-failed`案内）。
  `pyfltr.cli.configure_text_output(stream, *, level)`でformat別にstream/levelを切り替える:
  - `text` / `github-annotations` → stdout / INFO
  - `jsonl` + stdout → stderr / WARN
  - `sarif` + stdout → stderr / INFO
  - `code-quality` + stdout → stderr / INFO
  - `jsonl` / `sarif` / `code-quality` + `--output-file`指定 → stdout / INFO
  - MCP経路（`run_pipeline(force_text_on_stderr=True)`）→ stderr / INFO
- `pyfltr.structured`: JSONL / SARIF / Code Qualityの構造化出力。`pyfltr.cli.configure_structured_output(dest)`で
  `StreamHandler(sys.stdout)`または`FileHandler(output_file)`に切り替える。
  `text` / `github-annotations`ではhandler未設定（構造化出力なし）

出力フォーマット分岐は`pyfltr/formatters.py`の`OutputFormatter` Protocol実装群に集約している（`FORMATTERS`レジストリから動的解決）。

JSONLはstdout / file両モードともcompletion順streamingに統一する。
stdout占有は`jsonl` / `sarif` / `code-quality`かつ`--output-file`未指定時のみ発生する。

## 注意点

- `uv run mkdocs build --strict`でリンク・nav整合性を検証（ただし日本語アンカーリンク`#見出し日本語`は
  MkDocs TOCで解決できずINFO通知のみで`--strict`でも検知されないため手動確認要）
- 内部リンクは英数アンカーを優先する。MkDocs（Material）のslugifyは英数のみを採用してアンカー生成する。
  markdownlint MD051は見出し原文を見るため、`{#id}`記法で明示併設する（例:「### jsonl形式の使い方 {#jsonl}」）
- `docs/guide/index.md`の対応ツール一覧と`mkdocs.yml`内llmstxt `markdown_description`の「対応ツール」節は
  人手同期（SSOT化しない運用）
- `mkdocs.yml`内llmstxt `markdown_description`にはLLMが利用する際に有用な情報のみ記載する
 （`run-for-agent`サブコマンド、主要オプションなど）。LLMにとって不要な情報はdocs側をSSOTとし、多重管理を避ける
- ドキュメント構成変更時は`docs/development/development.md`の「READMEとdocsの役割分担」節を先に参照
