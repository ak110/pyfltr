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
