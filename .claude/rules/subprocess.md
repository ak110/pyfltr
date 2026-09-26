---
paths:
  - "pyfltr/command/process.py"
  - "pyfltr/command/env.py"
  - "pyfltr/command/runner.py"
  - "pyfltr/command/mise.py"
  - "pyfltr/cli/main.py"
  - "tests/process_test.py"
---

# pyfltrのsubprocess関連の方針

- CLI起動時に`os.environ["PATH"]`を順序先勝ちで重複排除する。
  CLIからの呼び出し時に限って実行し、ライブラリ用途では実行しない。詳細は`pyfltr/cli/main.py`の`main()`docstring
  および`pyfltr/command/env.py`の`dedupe_environ_path`に集約する
- mise経由のsubprocess（`bin-runner = "mise"`等）に限り、PATHからmiseが注入したtoolパスを除外したenvを渡す。
  `ensure_mise_available`内の`mise exec --version` / `mise trust`にも同じ除外envを明示的に渡す。
  対症療法であり、mise側の修正後は撤去または維持を再検討する余地がある。
  詳細は`pyfltr/command/env.py`の`build_subprocess_env` / `build_mise_subprocess_env`に集約する
- subprocessの出力をテキストで取得する場合は`encoding`と`errors`を明示する。
  実行ホストの既定文字コードへ委ねると、CP932環境ではreader thread内の`UnicodeDecodeError`が
  ツールの実行が成功したときに表面化する。指定は`encoding="utf-8"` / `errors="backslashreplace"`で揃え、
  実装は`pyfltr/command/process.py`の`run_subprocess`と`pyfltr/command/mise.py`の
  `run_mise_with_trust`に集約する
- subprocess経過時間ベースのtimeout監視は`threading.Timer`分離方式で組む。
  本体ループの非ブロック化は不要、停止後はEOF到達で解放される標準パターンに揃える。
  実装パターンは`pyfltr/command/process.py`の`run_subprocess` / `_on_timeout` / `_kill_process_tree`を参照する
