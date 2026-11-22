import copy
import dataclasses
import os
import pathlib
import typing

import platformdirs

import pyfltr.tool_types
import pyfltr.tools
import pyfltr.warnings_

# global優先キーのSSOT。
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
`pyfltr.command.invocation.expanduser_args` を経由する。

展開タイミングはsubprocess引数組み立て直前（`pyfltr.command.runner.build_commandline` /
`build_invocation_argv` および `pyfltr.command.two_step.base` /
`pyfltr.command.two_step.prettier` の各経路）で、
`config.values` 読込時点では原文を保持する（`command-info` サブコマンドの
`configured_path` / `configured_args` / `configured_extend_args` にも原文が露出する）。
"""


DEFAULT_CONFIG: dict[str, typing.Any] = {
    "preset": "",
    "env-file": "",
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
        # aube-lock.yamlはmiseが内蔵するnpm互換パッケージマネージャーaubeが`.mise/locks/`配下へ書く。
        "Gemfile.lock",
        "Pipfile.lock",
        "aube-lock.yaml",
        "composer.lock",
        "npm-shrinkwrap.json",
        "package-lock.json",
        "pnpm-lock.yaml",
        "poetry.lock",
        "uv.lock",
        "yarn.lock",
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


def register_command_dynamic_defaults(
    defaults: dict[str, typing.Any],
    builtin_commands: dict[str, pyfltr.tool_types.CommandInfo],
) -> None:
    for command, info in builtin_commands.items():
        defaults.setdefault(f"{command}-targets", copy.deepcopy(info.targets))
        defaults.setdefault(f"{command}-extend-targets", [])
        defaults.setdefault(f"{command}-exclude", [])
        defaults.setdefault(f"{command}-extend-args", [])


register_command_dynamic_defaults(DEFAULT_CONFIG, pyfltr.tools.BUILTIN_COMMANDS)


# （pyrightの `reportPossiblyUnboundVariable` 誤検知も同時に回避）。
def register_command_timeout_defaults(defaults: dict[str, typing.Any], command_names: list[str]) -> None:
    for command in command_names:
        defaults[f"{command}-timeout"] = -1


register_command_timeout_defaults(DEFAULT_CONFIG, pyfltr.tools.BUILTIN_COMMAND_NAMES)


def register_command_severity_defaults(defaults: dict[str, typing.Any], command_names: list[str]) -> None:
    for command in command_names:
        defaults.setdefault(f"{command}-severity", "error")


def register_command_hints_defaults(defaults: dict[str, typing.Any], command_names: list[str]) -> None:
    for command in command_names:
        defaults[f"{command}-hints"] = []


register_command_severity_defaults(DEFAULT_CONFIG, pyfltr.tools.BUILTIN_COMMAND_NAMES)


register_command_hints_defaults(DEFAULT_CONFIG, pyfltr.tools.BUILTIN_COMMAND_NAMES)


def register_command_subproject_aware_defaults(
    defaults: dict[str, typing.Any],
    builtin_commands: dict[str, pyfltr.tool_types.CommandInfo],
) -> None:
    for command, info in builtin_commands.items():
        defaults[f"{command}-subproject-aware"] = info.subproject_aware


register_command_subproject_aware_defaults(DEFAULT_CONFIG, pyfltr.tools.BUILTIN_COMMANDS)


def resolve_subproject_aware(values: dict[str, typing.Any], command: str, default: bool) -> bool:
    raw = command_setting(values, command, "subproject-aware", default)
    if isinstance(raw, bool):
        return raw
    return default


def resolve_severity(values: dict[str, typing.Any], command: str) -> str:
    raw = command_setting(values, command, "severity", "error")
    if raw in SEVERITY_VALUES:
        return str(raw)
    return "error"


def resolve_command_timeout(values: dict[str, typing.Any], command: str) -> float | None:
    per_tool_raw = command_setting(values, command, "timeout", -1)
    try:
        per_tool = int(per_tool_raw)
    except (TypeError, ValueError):
        per_tool = -1
    if per_tool >= 0:
        return float(per_tool) if per_tool > 0 else None
    global_raw = values.get("command-timeout", 0)
    try:
        global_value = int(global_raw)
    except (TypeError, ValueError):
        global_value = 0
    return float(global_value) if global_value > 0 else None


def resolve_retry_kwargs(values: dict[str, typing.Any]) -> dict[str, typing.Any]:
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
    def __call__(self, key: str, *, message: str) -> None:
        pass


@dataclasses.dataclass(frozen=True)
class Config:
    values: dict[str, typing.Any]
    commands: dict[str, pyfltr.tool_types.CommandInfo]
    """ビルトイン + カスタムの統合コマンドレジストリ"""
    command_names: list[str]
    """コマンドの並び順リスト（ビルトイン順 → カスタムコマンド順）"""
    warned_global_only_entries: frozenset[ConfigWarningEntry] = frozenset()
    """このロードで実際に発行し、かつ対象キーの由来が`{"global"}`（project側で上書きして
    いない）だった検証警告の集合。`resolve_subproject_configs`が次のロードへの抑止対象として
    引き継ぐために参照する。"""

    def __getitem__(self, key: str) -> typing.Any:
        return self.values[key]


def default_global_config_path() -> pathlib.Path:
    override = os.environ.get("PYFLTR_GLOBAL_CONFIG")
    if override:
        return pathlib.Path(override)
    return pathlib.Path(platformdirs.user_config_dir("pyfltr", appauthor=False)) / "config.toml"


def command_setting[SettingT](values: typing.Mapping[str, typing.Any], command: str, key: str, default: SettingT) -> SettingT:
    return typing.cast(SettingT, values.get(f"{command}-{key}", default))


def required_command_setting(values: typing.Mapping[str, typing.Any], command: str, key: str) -> typing.Any:
    return values[f"{command}-{key}"]
