"""設定の型・既定値・実行値の取得。"""

import copy
import dataclasses
import os
import pathlib
import typing

import platformdirs

import pyfltr.tools
import pyfltr.warnings_

# global優先キーのSSOT。
# archive/cache系の設定値はマシン単位で揃えたい性質のため、
# `~/.config/pyfltr/config.toml`（global設定）に書かれた値をproject側より優先する。
# 通常キーはproject優先（後勝ち）であるのに対して、本集合は逆向きの優先順を持つ。
# 範囲拡大時は `docs/guide/configuration.md` と関連テストも併せて更新する（人手同期）。
ARCHIVE_CONFIG_KEYS: frozenset[str] = frozenset(
    {
        "archive",
        "archive-max-runs",
        "archive-max-size-mb",
        "archive-max-age-days",
    }
)


CACHE_CONFIG_KEYS: frozenset[str] = frozenset({"cache", "cache-max-age-hours"})


GLOBAL_PRIORITY_KEYS: frozenset[str] = ARCHIVE_CONFIG_KEYS | CACHE_CONFIG_KEYS


SEVERITY_VALUES: tuple[str, ...] = ("error", "warning")


"""`{command}-severity`に指定可能な値。

- `"error"`（既定）: 従来通り。失敗時にJSONL `status="failed"` を返し、パイプライン全体のexit codeも非0となる
- `"warning"`: 失敗時にJSONL `status="warning"` を返す。`commands_summary.needs_action.warning` に集計するが、
  `failure_present` 判定からは除外するため `summary.guidance` のfailure系は出力されず、パイプラインのexit codeにも影響しない
"""


EXPAND_USER_KEY_SUFFIXES: tuple[str, ...] = (
    "-path",
    "-args",
    "-extend-args",
    "-lint-args",
    "-fix-args",
    "-check-args",
    "-write-args",
)


"""`~`展開を適用する設定キーのサフィックス集合。

利用者ホームディレクトリ依存のパス（例: `~/dotfiles/.../tool.py`）を設定値として
記述できるようにするため、特定のper-toolキーに限り `~` 展開を適用する。
本集合は対象サフィックスのSSOTで、`ruff-format-check-args` のような固定名キーも
`-check-args` サフィックスで吸収する。

`config-files` / `targets` / `{command}-extend-targets` 等のglobパターン用キーは
glob内チルダの意図しない展開を防ぐため対象外とする。

展開規則は要素先頭の `~` に加え、要素内の最初の `=` 直後の `~` も展開する
（`--config=~/cfg.toml` を `--config=<HOME>/cfg.toml` に展開）。
`os.path.expanduser` は先頭の `~` のみ展開するため、展開は
`pyfltr.command.runner.expanduser_args` を経由する。

展開タイミングはsubprocess引数組み立て直前（`pyfltr.command.runner.build_commandline` /
`build_invocation_argv` および `pyfltr.command.two_step.base` /
`pyfltr.command.two_step.prettier` の各経路）で、
`config.values` 読込時点では原文を保持する（`command-info` サブコマンドの
`configured_path` / `configured_args` / `configured_extend_args` にも原文が露出する）。
"""


DEFAULT_CONFIG: dict[str, typing.Any] = {
    "preset": "",
    "python": False,
    "javascript": False,
    "rust": False,
    "dotnet": False,
    "exclude-fence-under": [],
    "python-runner": "uv",
    "js-runner": "pnpx",
    "bin-runner": "mise",
    "mise-auto-trust": True,
    "archive": True,
    "archive-max-runs": 100,
    "archive-max-size-mb": 1024,
    "archive-max-age-days": 30,
    "replace-history-max-entries": 100,
    "replace-history-max-size-bytes": 200 * 1024 * 1024,
    "replace-history-max-age-days": 30,
    "jsonl-diagnostic-limit": 0,
    "jsonl-message-max-lines": 30,
    "jsonl-message-max-chars": 2000,
    "cache": True,
    "cache-max-age-hours": 12,
    "jobs": 8,
    "command-timeout": 600,
    "retry-on-oom": True,
    "retry-max-attempts": 1,
    "exclude": [
        # 値はflake8・blackの既定値および以下のgitignoreテンプレートを参考に選定する。
        # https://github.com/github/gitignore/blob/master/Python.gitignore
        # https://github.com/github/gitignore/blob/main/Node.gitignore
        "*.egg",
        "*.egg-info",
        ".aider*",
        ".bzr",
        ".cache",
        ".cursor",
        ".direnv",
        ".eggs",
        ".git",
        ".hg",
        ".idea",
        ".mypy_cache",
        ".nox",
        ".pnpm",
        ".pyre",
        ".pytest_cache",
        ".ruff_cache",
        ".serena",
        ".svn",
        ".tox",
        ".venv",
        ".vite",
        ".vscode",
        ".yarn",
        "CVS",
        "__pycache__",
        "__pypackages__",
        "_build",
        "buck-out",
        "build",
        "dist",
        "node_modules",
        "site",
        "venv",
        # バイナリファイル（テキスト系lintの対象外）
        "*.bmp",
        "*.dll",
        "*.dylib",
        "*.eot",
        "*.exe",
        "*.gif",
        "*.gz",
        "*.ico",
        "*.jpeg",
        "*.jpg",
        "*.mp3",
        "*.mp4",
        "*.otf",
        "*.pdf",
        "*.png",
        "*.so",
        "*.tar",
        "*.ttf",
        "*.wasm",
        "*.wav",
        "*.webp",
        "*.woff",
        "*.woff2",
        "*.zip",
        # ロックファイル
        "Gemfile.lock",
        "Pipfile.lock",
        "composer.lock",
        "package-lock.json",
        "pnpm-lock.yaml",
        "poetry.lock",
        "uv.lock",
        "yarn.lock",
        # 自動生成テキスト（minify済み・source map）
        "*.map",
        "*.min.css",
        "*.min.js",
    ],
    "extend-exclude": [],
    "respect-gitignore": True,
    "subproject-exclude": [],
    "subproject-use-gitignore": True,
    "subproject-uv-workspace": True,
    "aliases": {
        alias: [name for name, info in pyfltr.tools.BUILTIN_COMMANDS.items() if info.type == kind]
        for alias, kind in {"format": "formatter", "lint": "linter", "test": "tester"}.items()
    }
    | {"audit": [name for name, info in pyfltr.tools.BUILTIN_COMMANDS.items() if "audit" in info.aliases]},
}


for _tool_info in pyfltr.tools.BUILTIN_COMMANDS.values():
    DEFAULT_CONFIG.update(_tool_info.defaults)


# デフォルト設定。


def register_command_dynamic_defaults(
    defaults: dict[str, typing.Any],
    builtin_commands: dict[str, pyfltr.tools.CommandInfo],
) -> None:
    """全ビルトインコマンドへ実行時に受理する動的設定キーの既定値を登録する。

    ツール固有の固定既定値が先に登録されている場合は、その値を保持する。
    """
    for command, info in builtin_commands.items():
        defaults.setdefault(f"{command}-targets", copy.deepcopy(info.targets))
        defaults.setdefault(f"{command}-extend-targets", [])
        defaults.setdefault(f"{command}-exclude", [])
        defaults.setdefault(f"{command}-extend-args", [])


register_command_dynamic_defaults(DEFAULT_CONFIG, pyfltr.tools.BUILTIN_COMMANDS)


# per-tool `{command}-timeout` キーをビルトインコマンドぶん追加する。
# 既定値 `-1` は「未設定」を意味するsentinelで、解決時にグローバル `command-timeout` 値へフォールバックする。
# `0` 以下を明示指定した場合は対象のコマンドのtimeoutを無効化する。
# `>0` の場合は秒数を指定する。`per-tool` 値が `-1` 以外なら本値を優先する。
# 命名は既存の `{command}-args` / `{command}-fast` 系と同パターンに揃える。
# 別のper-toolキーで「未指定でグローバル値へフォールバック」を表現する場合も同じ `-1`
# sentinel運用に揃える。`None` 表現はTOML上の素直な記述方法が無く、整数フィールド
# としての一貫性も崩れるため採用しない。
# モジュールトップレベルでの `for` ループ変数のスコープ漏れを避けるため関数経由で適用する
# （pyrightの `reportPossiblyUnboundVariable` 誤検知も同時に回避）。
def register_command_timeout_defaults(defaults: dict[str, typing.Any], command_names: list[str]) -> None:
    """全ビルトインコマンドへ `{command}-timeout = -1` のsentinel既定値を登録する。"""
    for command in command_names:
        defaults[f"{command}-timeout"] = -1


register_command_timeout_defaults(DEFAULT_CONFIG, pyfltr.tools.BUILTIN_COMMAND_NAMES)


def register_command_severity_defaults(defaults: dict[str, typing.Any], command_names: list[str]) -> None:
    """全ビルトインコマンドへ `{command}-severity = "error"` の既定値を登録する。

    `severity = "warning"` 設定下では従来 `failed` 扱いの結果がJSONL上 `status="warning"` に切り替わる。
    既定値 `"error"` は従来挙動を維持するためのもので、変更したい場合のみ
    `pyproject.toml`/global設定で個別に指定する。
    """
    for command in command_names:
        defaults[f"{command}-severity"] = "error"


def register_command_hints_defaults(defaults: dict[str, typing.Any], command_names: list[str]) -> None:
    """全ビルトインコマンドへ `{command}-hints = []` の既定値を登録する。

    指摘1件以上のときに限りJSONL `command.hints` の `user.<n>` キーへ
    順番に追加される。既定の空配列はLLM入力にhintを追加しない挙動を意味する。
    """
    for command in command_names:
        defaults[f"{command}-hints"] = []


register_command_severity_defaults(DEFAULT_CONFIG, pyfltr.tools.BUILTIN_COMMAND_NAMES)


# colloquial-checkは口語表現の指摘であり、CI/pre-commitを止めない`warning`扱いを既定とする。
DEFAULT_CONFIG["colloquial-check-severity"] = "warning"


register_command_hints_defaults(DEFAULT_CONFIG, pyfltr.tools.BUILTIN_COMMAND_NAMES)


def register_command_subproject_aware_defaults(
    defaults: dict[str, typing.Any],
    builtin_commands: dict[str, pyfltr.tools.CommandInfo],
) -> None:
    """全ビルトインコマンドへ `{command}-subproject-aware` 既定値を登録する。

    既定値は `pyfltr.tools.CommandInfo.subproject_aware` から取得する。利用者は
    `pyproject.toml` で `{command}-subproject-aware = false` 等と上書きできる。
    """
    for command, info in builtin_commands.items():
        defaults[f"{command}-subproject-aware"] = info.subproject_aware


register_command_subproject_aware_defaults(DEFAULT_CONFIG, pyfltr.tools.BUILTIN_COMMANDS)


def resolve_subproject_aware(values: dict[str, typing.Any], command: str, default: bool) -> bool:
    """per-tool `{command}-subproject-aware` の有効値を返す。

    値が真偽値以外の場合は `default`（`pyfltr.tools.CommandInfo.subproject_aware`）を返す。
    バリデーションは `load_config` 側で行うため、ここでは型確認のみ行う安全側のヘルパー。
    """
    raw = command_setting(values, command, "subproject-aware", default)
    if isinstance(raw, bool):
        return raw
    return default


def resolve_severity(values: dict[str, typing.Any], command: str) -> str:
    """per-tool `{command}-severity` の有効値を返す。

    既定値 `"error"` は従来挙動と同じ。`"warning"` 設定時は
    `CommandResult.severity` フィールドへ転記され、`status` プロパティが
    通常失敗を `"warning"` に置き換える。未知の値は `"error"` として扱う
    （バリデーションは `load_config` 側で行う）。
    """
    raw = command_setting(values, command, "severity", "error")
    if raw in SEVERITY_VALUES:
        return str(raw)
    return "error"


def resolve_command_timeout(values: dict[str, typing.Any], command: str) -> float | None:
    """per-tool `{command}-timeout` とグローバル `command-timeout` から有効値を解決する。

    `{command}-timeout`の意味は次の通り。

    - `-1`（既定sentinel）または負値: 「未設定」を意味し、グローバル `command-timeout`
      の値へフォールバックする。利用者向けドキュメントでは「未指定」と表現する
    - `0`: 対象のper-toolのtimeoutを明示的に無効化する（戻り値`None`）
    - 正の整数: 対象の秒数で監視する

    グローバル`command-timeout`は次の通り。

    - `0`: 全コマンドのtimeoutを無効化する
    - 正の整数: per-tool未設定時の既定秒数として採用される

    `None` を返した場合 `pyfltr.command.process.run_subprocess` はtimeout監視を行わない。
    `float` を返した場合は対象の秒数で監視する。
    """
    per_tool_raw = command_setting(values, command, "timeout", -1)
    try:
        per_tool = int(per_tool_raw)
    except (TypeError, ValueError):
        per_tool = -1
    if per_tool >= 0:
        return float(per_tool) if per_tool > 0 else None
    # per-tool未設定（sentinel）→グローバル値へフォールバック
    global_raw = values.get("command-timeout", 0)
    try:
        global_value = int(global_raw)
    except (TypeError, ValueError):
        global_value = 0
    return float(global_value) if global_value > 0 else None


def resolve_retry_kwargs(values: dict[str, typing.Any]) -> dict[str, typing.Any]:
    """`run_process_loop()` へ展開するOOMリトライ用キーワード引数を返す。

    各subprocess呼び出し点で `**resolve_retry_kwargs(config.values)` 形で渡し、
    `retry-on-oom`・`retry-max-attempts` の参照と型変換を集約する。
    """
    return {
        "retry_on_oom": bool(values["retry-on-oom"]),
        "retry_max_attempts": int(values["retry-max-attempts"]),
    }


ConfigWarningEntry = tuple[str, str]


"""設定検証警告1件を識別する`(設定キー名, 警告本文)`の組。

キー名だけでは同一キーに対する原因の異なる警告
（例: `my-tool-severity`の「値が不正」と「認識できないキー」）を区別できず、
モノレポでの重複抑止が別原因の警告まで巻き込むため、本文まで含めて識別する。
"""


class ConfigWarningEmitter(typing.Protocol):
    """設定検証警告を発行する呼び出し規約。

    `make_config_warning_emitter`が生成し、各検証関数へ渡される。
    """

    def __call__(self, key: str, *, message: str) -> None:
        """`key`に紐づく検証警告`message`を発行する（抑止対象なら何もしない）。"""


@dataclasses.dataclass(frozen=True)
class Config:
    """pyfltr設定。"""

    values: dict[str, typing.Any]
    commands: dict[str, pyfltr.tools.CommandInfo]
    """ビルトイン + カスタムの統合コマンドレジストリ"""
    command_names: list[str]
    """コマンドの並び順リスト（ビルトイン順 → カスタムコマンド順）"""
    warned_global_only_entries: frozenset[ConfigWarningEntry] = frozenset()
    """このロードで実際に発行し、かつ対象キーの由来が`{"global"}`（project側で上書きして
    いない）だった検証警告の集合。`resolve_subproject_configs`が次のロードへの抑止対象として
    引き継ぐために参照する。"""

    def __getitem__(self, key: str) -> typing.Any:
        """設定値を取得。"""
        return self.values[key]


def default_global_config_path() -> pathlib.Path:
    r"""XDG準拠のグローバル設定ファイルパスを返す。

    Linuxでは`~/.config/pyfltr/config.toml`、macOSでは
    `~/Library/Application Support/pyfltr/config.toml`、
    Windowsでは`%LOCALAPPDATA%\pyfltr\config.toml`になる。
    環境変数`PYFLTR_GLOBAL_CONFIG`が設定されていればそれを優先する
    （テスト容易性確保とユーザーの強制上書き用。`PYFLTR_CACHE_DIR`と命名対称）。

    `appauthor=False`を渡すのは、未指定時にWindowsで`appname`が
    appauthorとしても付与され`%LOCALAPPDATA%\pyfltr\pyfltr\config.toml`に
    なる挙動を回避するため。
    """
    override = os.environ.get("PYFLTR_GLOBAL_CONFIG")
    if override:
        return pathlib.Path(override)
    return pathlib.Path(platformdirs.user_config_dir("pyfltr", appauthor=False)) / "config.toml"


_SettingT = typing.TypeVar("_SettingT")


def command_setting(values: typing.Mapping[str, typing.Any], command: str, key: str, default: _SettingT) -> _SettingT:
    """検証済みのコマンド設定を既定値と同じ型で取得する。"""
    return typing.cast(_SettingT, values.get(f"{command}-{key}", default))


def required_command_setting(values: typing.Mapping[str, typing.Any], command: str, key: str) -> typing.Any:
    """カスタム定義も含む必須コマンド設定を取得する。"""
    return values[f"{command}-{key}"]
