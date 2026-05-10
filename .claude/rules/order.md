---
paths:
  - "pyfltr/command/builtin.py"
  - "pyfltr/config/config.py"
---

# pyfltrのコマンド並び順方針

`pyfltr/command/builtin.py`の`BUILTIN_COMMANDS`登録順はTUI・JSONL・command-info等の表示順と、
formatter群の実行順を兼ねる。
linter/tester群の実行順はLPT並列（推定実行時間の降順スケジューリング）で別管理されるため、
登録順は表示順としてのみ作用する。

## formatter群

純粋formatterを先に、lint的性質を持つものを後ろに配置する。最後尾は`pre-commit`とする
（リポジトリ固有の各種チェックが幅広く実行されるため、他formatterで直せるものを先に修正してから呼ぶ意図）。

- 先頭: `prettier`。多言語formatterで影響範囲が広く、最初に整形すると後続判定が安定する
- 中段: `ruff-format`・`uv-sort`・`shfmt`・`taplo`・`cargo-fmt`・`dotnet-format`（決定論的に整形する純粋formatter）
- 末尾: `pre-commit`

`pyfltr/config/config.py`の`DEFAULT_CONFIG["aliases"]["format"]`も同順に揃える。
`resolve_aliases()`が`command_names.index`で再ソートするため実行順への影響は無いが、SSOT観点で登録順と一致させる。

## linter/tester群

実行順はLPT並列で別管理されるため、表示順のみを「モダン順（後ろほど新しい）」に並べる。

- Python: `pylint`→`mypy`→`ruff-check`→`pyright`→`ty`
- JS/TS: `tsc`→`eslint`→`biome`→`oxlint`
- tester: `pytest`→`vitest`→`cargo-test`→`dotnet-test`

Rust（`cargo-clippy`・`cargo-check`・`cargo-deny`）はサブコマンドの機能差中心で並び替えの意義が薄い。
汎用ツール群は「モダン順」の判定が困難なため現状の登録順を維持する。
対象は`ec`・`typos`・`shellcheck`・`actionlint`・`hadolint`・`yamllint`・
`gitleaks`・`glab-ci-lint`・`markdownlint`・`textlint`である。
