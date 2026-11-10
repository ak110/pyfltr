"""ビルトインコマンドの登録順と導出定義。"""

import typing

import pyfltr.tool_defs.audit
import pyfltr.tool_defs.document_linters
import pyfltr.tool_defs.formatters
import pyfltr.tool_defs.general_linters
import pyfltr.tool_defs.javascript_linters
import pyfltr.tool_defs.native_linters
import pyfltr.tool_defs.python_linters
import pyfltr.tool_defs.sql_linters
import pyfltr.tool_defs.testers


def command_index(command_names: typing.Sequence[str], command: str) -> int:
    """定義順の位置を返す。未登録コマンドは末尾へ置く。"""
    return command_names.index(command) if command in command_names else len(command_names)


# 登録順は表示順とformatterの実行順を兼ねる（.claude/rules/order.md）。
BUILTIN_COMMANDS = {
    **pyfltr.tool_defs.formatters.COMMANDS,
    **pyfltr.tool_defs.general_linters.COMMANDS,
    **pyfltr.tool_defs.python_linters.COMMANDS,
    **pyfltr.tool_defs.document_linters.COMMANDS,
    **pyfltr.tool_defs.javascript_linters.COMMANDS,
    **pyfltr.tool_defs.native_linters.COMMANDS,
    **pyfltr.tool_defs.sql_linters.COMMANDS,
    **pyfltr.tool_defs.audit.COMMANDS,
    **pyfltr.tool_defs.testers.COMMANDS,
}

BUILTIN_COMMAND_NAMES: list[str] = list(BUILTIN_COMMANDS.keys())
"""ビルトインコマンドの名前リスト。"""


PYTHON_RUNNERS: tuple[str, ...] = ("direct", "uv", "uvx")
"""グローバル`python-runner`設定で指定できる値。

- `"direct"`: `shutil.which`で本体依存に同梱されたバイナリを直接呼ぶ
- `"uv"`:     cwdに`uv.lock`があり、かつ`uv`バイナリが利用可能な場合は
              `uv run --frozen <bin>`経由でプロジェクトのvenvにあるツールを呼ぶ。
              いずれかが満たされなければdirectへフォールバック
- `"uvx"`:    `uvx <bin>`形式でPyPI最新版を都度取得して起動する。
              `uv.lock`は参照せず、`{command}-version`設定とも連動しない
"""

JS_RUNNERS: tuple[str, ...] = ("pnpx", "pnpm", "npm", "npx", "yarn", "direct")
"""グローバル`js-runner`設定で指定できる値。"""

BIN_RUNNERS: tuple[str, ...] = ("direct", "mise")
"""グローバル`bin-runner`設定で指定できる値。"""

COMMAND_RUNNERS: tuple[str, ...] = (
    # カテゴリ委譲値（3値）
    "python-runner",
    "js-runner",
    "bin-runner",
    # 直接指定値（9値）
    "direct",
    "mise",
    "uv",
    "uvx",
    "pnpx",
    "pnpm",
    "npm",
    "npx",
    "yarn",
)
"""`{command}-runner`設定で指定できる値（対称12値）。

カテゴリ委譲値とper-tool直接指定値を対等な選択肢として並べる。
両者は対称で、利用者は委譲とper-toolオーバーライドを自由に選べる。

- カテゴリ委譲値（グローバル設定へ委譲）:
    - `"python-runner"`: グローバル`python-runner`設定（`direct` / `uv` / `uvx`）へ委譲する
    - `"js-runner"`:     グローバル`js-runner`設定（`pnpx` / `pnpm` / `npm` / `npx` / `yarn` / `direct`）へ委譲する
    - `"bin-runner"`:    グローバル`bin-runner`設定（`mise` / `direct`）へ委譲する
- 直接指定値（per-toolで実装ツールを直接指定）:
    - `"direct"`: `{command}-path`またはbin名で直接実行する
    - `"mise"`:   `mise exec <backend>@<version> -- <bin>`で実行する
    - `"uv"`:     cwdに`uv.lock`があり、かつ`uv`バイナリが利用可能な場合は
                  `uv run --frozen <bin>`経由で起動する。いずれかが満たされなければdirectへフォールバック
    - `"uvx"`:    `uvx <bin>`形式でPyPI最新版を都度取得して起動する。
                  `uv.lock`は参照せず、`{command}-version`設定とも連動しない
    - `"pnpx"` / `"pnpm"` / `"npm"` / `"npx"` / `"yarn"`: 各JSパッケージマネージャー経由で起動する

カテゴリ横断の組み合わせ（例: Python系ツールに`pnpm`を指定）はバリデーションでは拒否しない。
無意味な組み合わせは実行時の解決ロジックがエラー終了する。
"""

PYTHON_COMMANDS = tuple(command for command, info in BUILTIN_COMMANDS.items() if info.language == "python")
"""python 設定に紐づく Python 系コマンドの一覧。

Python 系ツール一式は本体依存（`dependencies`）に同梱済みで、
`uvx pyfltr` 単発で揃う。`{command}-runner = "python-runner"` 既定（グローバル `python-runner = "uv"` 既定経由）により、
cwdに`uv.lock`がある場合は利用者プロジェクトのuv環境のツール版が優先される。

言語カテゴリキー`python`のgate対象で、preset内でTrueとなっているツールを
通過させる。`python = false`または未指定のときは、preset由来でTrueになった
コマンドも個別`{command} = true`指定がなければFalseに上書きされる。
個別`{command} = true`はgateを越えて優先される。
`ty`のみpreset非収録のため、使用時は個別に`ty = true`を指定する運用を維持する。"""

JAVASCRIPT_COMMANDS = tuple(command for command, info in BUILTIN_COMMANDS.items() if info.language == "javascript")
"""javascript 設定に紐づく JavaScript / TypeScript 系コマンドの一覧。

TypeScriptはJavaScriptエコシステム上のツール群（eslint / prettier / tsc等）で
扱うため、専用カテゴリは設けずここに内包する。言語カテゴリキー`javascript`の
gate対象で、preset内でTrueとなっているツールを通過させる。挙動は
`PYTHON_COMMANDS`と同じ。"""

RUST_COMMANDS = tuple(command for command, info in BUILTIN_COMMANDS.items() if info.language == "rust")
"""rust 設定に紐づく Rust 系コマンドの一覧。

言語カテゴリキー`rust`のgate対象で、preset内でTrueとなっているツールを
通過させる。挙動は`PYTHON_COMMANDS`と同じ。"""

DOTNET_COMMANDS = tuple(command for command, info in BUILTIN_COMMANDS.items() if info.language == "dotnet")
"""dotnet 設定に紐づく .NET 系コマンドの一覧。

言語カテゴリキー`dotnet`のgate対象で、preset内でTrueとなっているツールを
通過させる。挙動は`PYTHON_COMMANDS`と同じ。"""

LANGUAGE_CATEGORIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("python", PYTHON_COMMANDS),
    ("javascript", JAVASCRIPT_COMMANDS),
    ("rust", RUST_COMMANDS),
    ("dotnet", DOTNET_COMMANDS),
)
"""言語カテゴリキーと対応するコマンド群の対応表。

preset適用後のgate処理（カテゴリFalseのときpreset由来の該当コマンドTrueを
Falseに上書きする）で共通に使う。"""

REMOVED_COMMANDS: frozenset[str] = frozenset({"pyupgrade", "autoflake", "isort", "black", "pflake8"})
"""v3.0.0 で削除されたコマンド名。

設定ファイル中に関連キー（`pyupgrade = true` / `black-args = [...]`など）を検出した
場合、`load_config`が案内付きのValueErrorを送出して移行を促す。"""

AUTO_ARGS = {command: info.auto_args for command, info in BUILTIN_COMMANDS.items() if info.auto_args}
"""コマンドごとの自動引数マッピング。

各タプルは（設定キー, 引数リスト）の対。設定キーがTrueの場合、
引数リストをコマンドライン先頭に自動挿入する。ユーザーの`*-args`と
重複する場合はスキップする。
"""

AUTO_VALUE_ARGS = {command: info.auto_value_args for command, info in BUILTIN_COMMANDS.items() if info.auto_value_args}
"""コマンドごとの数値を伴う自動引数マッピング。

各タプルは（設定キー, 引数テンプレート）の対。設定値が正の整数の場合、
`{value}`を展開した引数をコマンドライン先頭に自動挿入する。
`0`以下はツール側の既定値へ委ねる意味とし、引数を挿入しない。
ユーザーが`*-args`等で同じフラグを指定済みの場合もスキップする。

真偽値の`AUTO_ARGS`と分けるのは、既定値の意味（挿入する/しない）ではなく
数値そのものを利用者が調整する設定だからである。
"""
