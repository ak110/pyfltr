# AGENTS.md: pyfltr

多言語プロジェクトの品質チェックを一元管理し、コーディングエージェントから扱える形で提供する基盤。
組み込みのformatter・linter・testerとプロジェクト固有のカスタムチェックを同じ手順で実行できる。
JSON Lines出力（`--output-format=jsonl`）とMCPサーバー（`pyfltr mcp`）を利用できる。

## 開発手順

- `make update`: 依存更新 + prek autoupdate + pinactアクション更新 + 全テスト実行
  - `make update-actions`: GitHub Actionsのハッシュピン更新のみ（mise経由でpinact実行）
    - pinactは`.github/workflows/`配下だけを更新する
    - ピンが変わった場合は、利用者向けドキュメントのCI設定例も確認する
      - 対象は`docs/guide/recommended.md`と`docs/guide/configuration-tools.md`
      - アクションのメジャー版表記がピン版と一致しなければ、同じ変更に含めて更新する
- 推奨ガイド（`docs/guide/recommended.md`・`docs/guide/recommended-nonpython.md`）と本リポジトリの設定は、
  双方の変更時に対応する既存設定へ同じ変更を反映する
  - 推奨ガイドを変更した場合は、設定例が提示する対象のうち本リポジトリに存在するものへ反映する。
    設定例の新規追加でも、`before_script`等の準備手順を含めて本リポジトリの設定と差分を照合する
  - 本リポジトリを変更した場合は、対応する設定例が推奨ガイドに存在するとき、その例へ反映する
  - 本リポジトリ固有の事情で反映しない場合は、当該設定ファイルへ理由をコメントで残す
  - 推奨ガイドへ設定例を追加・変更する際、本リポジトリに対応する設定が存在しない場合は、
    当該例が成立するために必要な実行環境の前提条件を本文へ明示する
    - 前提条件には実行イメージへ用意すべきランタイム、チェックアウトの深度、必要な認証情報などを含める
    - 前提条件を確定できない場合は設定例を追加しない
- `docs/guide/`配下の利用者向け文書へ対応ツールの挙動を記述する場合は、記述する挙動を実測で裏付ける
  - 設定例を伴う記述は当該設定例と同じ経路で再現して確認する。
    設定例を伴わない記述は、当該挙動が生じる最小の構成を用意して再現する
  - 同一の指定でも、設定ファイルへ書く場合とコマンドラインで渡す場合とで挙動が変わることがある
  - 本リポジトリが同じ設定を採用している項目は、本リポジトリでの実行結果を裏付けの第一候補とする
  - 再現に別の設定ファイルを要する場合は、当該ツールを直接起動してよい。
    対象は挙動の確認に限り、確認後は通常どおりpyfltr経由の実行へ戻す
- `docs/guide/`配下の利用者向け文書へ測定値を記載する場合は、実行条件で変動しうる量を
  具体値ではなく変動する性質として記述する
  - 変動しない量を具体値で記載する場合は、同一文書内に同種の測定値が既にあるかを確認し、
    条件が重なるときはいずれかへ統一するか測定時点と測定条件を明示する
- リリース手順: `gh workflow run release.yaml --field=bump=PATCH`（`PATCH`は`MINOR`・`MAJOR`に変更可）
- Docker再ビルド単発起動: `gh workflow run docker-build.yaml`
  - `ghcr.io/ak110/pyfltr:latest`をリリースを伴わず更新する
  - `--field=version=X.Y.Z`で特定バージョンを指定する。未指定時はPyPI最新公開版を採用する
- テストコードは`pyfltr/xxx_.py`に対して`tests/xxx_test.py`として配置する
- コミット前の検証方法: `make test`
  - formatter・linter・tester・カスタムチェックなど対応ツール全般を個別に直接起動せず、
    `make test`または`uv run pyfltr run <path>`を使う。
    設定で無効化しているツールも直接起動すれば動作するため、設定による無効化は直接起動への防御にならない
    - 例外は利用者向け文書の挙動記述を裏付ける場合に限る（本章の挙動裏付けに関する規定を参照）
  - 特定ファイルのみを対象にする場合は`uv run pyfltr run <path>`にパスを渡す
  - 修正後の再実行時は`--commands=mypy,ruff-check`等で限定して実行する（最終検証はCIに委ねる前提）
  - 診断内容の調査・検体収集を目的として対応ツールを実行する場合は、
    自動修正を抑止する`--no-fix`（MCPでは`no_fix`）を併せて渡す。
    修正を目的としない実行で対象ファイルが書き換わると、調査対象の診断が消え、
    利用者の未コミット変更との切り分けも要する
    - `--no-fix`が抑止するのはfixステージ（`{command}-fix-args`を持つlinter）だけであり、
      formatterは通常ステージで対象ファイルを書き込む。
      `ruff-format-by-check`が既定で有効なため、`ruff-format`は`--no-fix`の指定時も
      `check --fix --unsafe-fixes`を先に実行し、未使用importの削除など整形以外の修正まで及ぶ
    - 書き換えを避けたい場合は`--no-fix`に加えて`--commands`で対象をlinterへ限定する
  - CIとローカルではmiseが解決する対応ツールの版が異なる場合がある
    - CIでのみ再現した指摘を検証する場合は、`pyproject.toml`の`[tool.pyfltr]`へ
        `{command}-version = "<版>"`を一時的に指定して`uv run pyfltr run <path> --commands=<command>`を実行し、
        確認後に設定を元へ戻す
    - この手順でもツールの実行ファイル・コンテナーイメージの直接起動は避け、pyfltr経由での実行を保つ
    - 版を指定できるのはbin-runner対応ツールに限られる
        （[docs/guide/configuration-tools.md](docs/guide/configuration-tools.md)の「バージョン指定」を参照）
  - エージェント検出用の環境変数が設定された環境では`run`の出力形式が`jsonl`、
    静音モードが既定で有効になるため、`run-for-agent`を明示指定する必要はない
  - MCPサーバー（`pyfltr mcp`）を登録している環境では、CLI直接実行よりMCPツールを優先する。
    CLIで可能な操作は端末表示・出力先の制御、`--no-archive`、`--{tool}-args`群を除きMCPへ露出している

## アーキテクチャの参照先

サブパッケージ・モジュールごとの構成詳細とサブパッケージ間の依存方向、
format別のlogger stream/level切替の詳細は[docs/development/architecture.md](docs/development/architecture.md)を参照する。

## 実装上の不変条件

- `subproject_aware=True`ツールはサブプロジェクトループの内側で動く前提で実装する。
  ツール起動時のcwdは`ExecutionContext.subproject_cwd`（指定時）または起点cwdを採用する
- subprocess・git・mise・ファイル走査などcwd依存処理はプロセスのcwdに依存しない実装にする。
  `subprocess.Popen(cwd=...)`の引数、または`start_cwd`・`base_cwd`・`cwd`等の
  明示引数でcwdを渡す。`os.chdir()`でグローバル状態を変更しない
- `pyfltr/command/core_.py`の`ExecutionParams.targets`と`CommandResult.target_files`は
  実際に処理対象となったファイル集合を表すフィールドである。
  マスク済み一時パスなど値の意味を変える差し替えをしない。
  一時パスへの差し替えが必要な用途は`ExecutionParams.cache_commandline`と
  `ExecutionParams.file_path_remap`で表現する
  （両フィールドの下流経路は[docs/development/architecture.md](docs/development/architecture.md)を参照する）
- サブプロジェクトごとに繰り返す処理が警告を発行する場合は、
  起点実行分を含めて同一の`source`と`message`の組が1件に収まることを確認する。
  モノレポ構成での実測手順は[docs/development/architecture.md](docs/development/architecture.md)の
  「モノレポ対応」節を参照する
- 外部ツールの成否は`pyfltr/command/core_.py`の`CommandResult.status`が終了コードから導出する。
  終了コード0は`errors`・`has_error`を参照せず`succeeded`となるため、
  `pyfltr/command/error_parser.py`が抽出した診断は正常終了したツールの成否を変えない
  （`has_error`が`status`へ影響するのは終了コードが非0のformatter分岐に限る）。
  ツールが期待する処理を実施しないまま正常終了する事象への対策は、出力解析ではなく
  `pyfltr/command/dispatcher.py`の`_prepare_execution_params`が呼ぶ実行前検査で
  `resolution_failed`へ倒す経路を採用する。
  `resolution_failed`は`status`の最優先分岐で確定するため、
  `{command}-severity = "warning"`による格下げの対象外とする。
  ツールの成否を変えず警告の発行に留める対策は、
  「実行前検査で`resolution_failed`へ倒す経路を採用する」規定の対象外とし、出力解析を採用してよい。
  ツール自身が競合や設定の無効化を出力へ明示する場合は、当該出力を読む方が
  pyfltr側で判定条件を再実装するより誤検出が生じにくい

## 注意点

- 対象ファイルに応じて`.claude/skills/`配下のpyfltr固有スキル
  （`test-constraints`・`output-format`・`tool-resolution`・`ssot`・
  `grep-replace`・`pyfltr-add-tool`）を呼び出す
- `pyfltr/command/error_parser.py`を変更した場合、`error-parser-reviewer`サブエージェントによる
  網羅レビューをコミット前に完了させる。事後レビューにすると不良を含むコミットが記録され、
  是正に追加のリリースを要する
  - 対象ファイルの変更を確定してから網羅レビューを委譲し、
    完了報告を受領するまで比較対象を維持する
  - 網羅レビューの結果は委譲した時点の実装に対する判定である。
    レビュー担当は直前のコミットの実装を変更前、作業ツリーの現物を変更後として比較する
  - 委譲から完了報告の受領までの間は、対象ファイルの変更とコミットを行わない
  - 委譲中に対象ファイルを変更した場合は、変更後の実装を対象として網羅レビューをやり直す
- `pyfltr/colloquial/words.txt`は冒頭コメントでコーディングエージェントによる読み込みを禁じている。
  編集時に限らず調査時も読み込まず、辞書内容の確認が必要な場合は`Explore`サブエージェントへ委譲する
  - 運用方針コメントの更新ではファイル全体を読み込む編集手段を使わず、
    `sed -i '<コメント終端行>a<挿入行>' <パス>`のように行番号を指定した挿入で編集する
  - コメント終端行は`grep -vn '^#' <パス> | head -1 | cut -d: -f1`が返す行番号から1を引いた値とする
  - 変更の確認では`sed -n '1,<コメント終端行>p' <パス>`でコメント範囲へ限定して表示し、
    `grep -c '' <パス>`で行数を照合する。辞書エントリー本体は表示させない
  - 読み込みを避ける理由は、辞書の検出語がコンテキストへ入るとエージェントの生成傾向へ混入し、
    口語表現の生成抑止という本チェッカーの目的と逆方向に作用するためである
