---
paths:
  - "pyfltr/command/targets.py"
  - "tests/command_core_test.py"
---

# pyfltrの対象ファイル収集方針

## シンボリックリンクの取り扱い

`expand_all_files`はシンボリックリンクディレクトリを辿る。
ディレクトリ走査の前に当該ディレクトリ自身が`is_symlink`かつ`respect-gitignore=True`の場合は、
末尾`/`を付けない単一パス指定で`git check-ignore`へ問い合わせ、ignoredならその配下を辿らない。
この早期スキップは`.gitignore`のファイル形式パターン（`name`・`*pattern*`等）に限り有効である。
`link/`形式のディレクトリ専用パターンに対しては、`git check-ignore`がシンボリックリンク越えのpathspecを
拒否する制限により判定不可となり、早期スキップは機能しない。
最終出力は実体パス単位で重複排除し、パス文字列で安定ソートする。
同一ファイルを複数のパスから列挙する構成（リポジトリ内のシンボリックリンク再配置など）で
ツールが同じ内容を多重チェックすることを避けるため。

## `_filter_by_gitignore`の挙動

cwdリポジトリ外のパスは判定対象外として残し、`returncode`が想定外の場合のサイレント素通しは採用せず`emit_warning`で通知する。
詳細は`pyfltr/command/targets.py`の`_filter_by_gitignore`のdocstringを参照する。
