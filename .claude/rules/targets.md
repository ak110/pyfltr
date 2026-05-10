---
paths:
  - "pyfltr/command/targets.py"
  - "tests/command_core_test.py"
---

# pyfltrの対象ファイル収集方針

- 対象ファイル収集は実体パス単位で重複排除し、パス文字列で安定ソートする。
  シンボリックリンク再配置等で同一実体が複数パスから列挙される構成でも多重チェックを避ける
- `respect-gitignore=True`下ではディレクトリ自身の`is_symlink()`を判定し、
  ignoredなら配下を辿らない早期スキップを採用する。
  シンボリックリンク扱いの詳細・早期スキップの限界（`link/`形式パターンへの非対応など）は
  `pyfltr/command/targets.py`の`expand_all_files` / `_filter_by_gitignore`のdocstringに集約する
- cwdリポジトリ外パスは判定対象外として残し、`returncode`想定外時の素通しは採用しない（`emit_warning`で通知）
