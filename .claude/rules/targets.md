---
paths:
  - "pyfltr/command/targets.py"
  - "tests/command_core_test.py"
---

# pyfltrの対象ファイル収集方針

- 対象ファイル収集は実体パス単位で重複排除し、パス文字列で安定ソートする。
  シンボリックリンク再配置等で同一実体が複数パスから列挙される構成でも多重チェックを避ける
- 同一実体に複数パスが紐付く場合は非シンボリックリンクのパスを優先して残す。
  prettier等の一部ツールは末端がシンボリックリンクのファイルを明示指定されると
  `Explicitly specified pattern "X" is a symbolic link`エラーで失敗するため
- `respect-gitignore=True`下ではディレクトリ自身の`is_symlink()`を判定し、
  ignoredなら配下を辿らない早期スキップを採用する。
  シンボリックリンク扱いの詳細・早期スキップの限界（`link/`形式パターンへの非対応など）は
  `pyfltr/command/targets.py`の`expand_all_files` / `_filter_by_gitignore`のdocstringに集約する
- cwdリポジトリ外パスは判定対象外として残し、`returncode`想定外時の素通しは採用しない（`emit_warning`で通知）
- モノレポ対応では、サブプロジェクト境界の子サブプロジェクト配下を親サブプロジェクトの集合から除外する。
  最終的なファイル所属判定（どのサブプロジェクトに割り当てるか）は最深一致で
  `pyfltr/command/subprojects.py`の`classify_files_by_subproject`に集約する。
  `expand_all_files`の`exclude_subdirs`引数は走査効率化の補助手段に留め、ファイル所属判定の唯一の根拠としない

## 外部パス（起点cwd配下にない絶対パス）の分類方針

`CommandInfo`の3フィールドで分類を表現する。
分類ロジックの集約先は2つの経路に分かれる。

- 非モノレポ経路: `pyfltr/command/dispatcher.py`の`_prepare_execution_params`で外部パスフィルタと
  `--config`注入を担う
- モノレポ経路: `_run_subproject_loop`で注入対象・素通し対象の追加実行と、除外対象ツールの警告発行を行う

- `--config`明示注入: `config_arg_template`と`config_inject_candidates`を指定するツール
 （`markdownlint`・`textlint`）。
  起点cwd直下を`config_inject_candidates`順に走査し、最初に見つかった設定ファイルの絶対パスを
  `commandline_prefix`直後に挿入する
  - 内部パスのみの実行でも一律で注入経路を通す
  - 利用者が`{command}-args`・`{command}-extend-args`・CLI`--{command}-args`のいずれかで
      `--config`を指定済みのときは注入をスキップする
- 外部パス除外＋警告: `allows_external_paths=False`を指定するツール
 （`pre-commit`・`pytest`・`vitest`・`cargo-test`・`dotnet-test`・`gitleaks`・`semgrep`・依存の脆弱性監査の各ツール）。
  対象から外部パスを除外し、各ファイルに`emit_warning` + `add_filtered_direct_file(reason="external")`を発行する
- 既定（素通し）: 上記以外。各ツールの設定探索仕様に委ねる

外部パス判定基準は「起点cwd配下にない絶対パス」で、`_is_external_path`に集約する。
モノレポ時は`classify_files_by_subproject`がサブプロジェクト辞書から外部パスを除いた上で
`ExecutionBaseContext.external_files`へ保持する。
注入対象および素通し対象ツールは`_run_subproject_loop`内で起点cwd（`subproject_cwd=None`）を使い、
外部パス専用の追加実行を行ったうえで結果を`CommandResult.merge`で集約する。
除外対象ツールでは外部パスを破棄して警告のみ発行する。

`CommandInfo.config_files`は`load_config`の設定不在時の警告とキャッシュキー算出に専用で、
`--config`注入候補（`config_inject_candidates`）とは責務を分離する。
