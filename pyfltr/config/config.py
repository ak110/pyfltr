import copy
import dataclasses
import pathlib
import typing

import tomlkit
import tomlkit.exceptions

import pyfltr.config.overrides
import pyfltr.config.presets
import pyfltr.tools
import pyfltr.warnings_
from pyfltr.config.model import (
    DEFAULT_CONFIG,
    GLOBAL_PRIORITY_KEYS,
    Config,
    ConfigWarningEmitter,
    ConfigWarningEntry,
    default_global_config_path,
)
from pyfltr.config.selection import build_fast_alias
from pyfltr.config.validation import (
    close_matches,
    japanese_type_label,
    normalize_config_values,
    recompute_fast_aliases,
    register_custom_commands,
    validate_config,
    warn_precommit_prek_conflict,
)


def create_default_config() -> Config:
    config = Config(
        values=copy.deepcopy(DEFAULT_CONFIG),
        commands=dict(pyfltr.tools.BUILTIN_COMMANDS),
        command_names=list(pyfltr.tools.BUILTIN_COMMAND_NAMES),
    )
    config.values["aliases"]["fast"] = build_fast_alias(config)
    return config


def read_config_text(path: pathlib.Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as e:
        raise ValueError(f"設定ファイルを読み込めません: {path}: {e}。ファイルの読み取り権限を確認してください") from e


def format_project_config_missing(path: pathlib.Path, *, use_global: str) -> str:
    return (
        f"pyproject.tomlが見つかりません: {path}。"
        f"プロジェクトのルートで実行するか、global設定（XDG準拠）に書く場合は {use_global} を指定してください"
    )


def format_config_error(error: Exception, *, retry: str) -> str:
    if _is_toml_syntax_error(error):
        return f"{error}。{config_error_action(error, retry=retry)}"
    return str(error)


def config_error_action(error: Exception, *, retry: str) -> str:
    if _is_toml_syntax_error(error):
        return f"表示されたファイルの該当位置をTOMLの構文に直し、{retry}を再実行してください"
    return f"表示された設定値を直し、{retry}を再実行してください"


def _is_toml_syntax_error(error: BaseException) -> bool:
    cause: BaseException | None = error
    while cause is not None:
        if isinstance(cause, tomlkit.exceptions.TOMLKitError):
            return True
        cause = cause.__cause__
    return False


def read_global_config(path: pathlib.Path) -> dict[str, typing.Any]:
    if not path.exists():
        return {}
    try:
        text = read_config_text(path)
        data = tomlkit.parse(text)
    except tomlkit.exceptions.TOMLKitError as e:
        raise ValueError(f"global設定ファイルのTOML構文が不正です: {path}: {e}") from e
    raw = data.get("tool", {})
    raw = raw.get("pyfltr", {}) if isinstance(raw, dict) else {}
    return unwrap_tomlkit(raw) if isinstance(raw, dict) else {}


def unwrap_tomlkit(value: typing.Any) -> typing.Any:
    if isinstance(value, dict):
        return {str(k): unwrap_tomlkit(v) for k, v in value.items()}
    if isinstance(value, list):
        return [unwrap_tomlkit(item) for item in value]
    unwrap = getattr(value, "unwrap", None)
    if callable(unwrap):
        return unwrap()
    return value


def merge_global_and_project(
    global_data: dict[str, typing.Any],
    project_data: dict[str, typing.Any],
) -> tuple[dict[str, typing.Any], dict[str, set[str]]]:
    normalized_global = {key.replace("_", "-"): value for key, value in global_data.items()}
    normalized_project = {key.replace("_", "-"): value for key, value in project_data.items()}

    key_sources: dict[str, set[str]] = {}
    for key in normalized_global:
        key_sources.setdefault(key, set()).add("global")
    for key in normalized_project:
        key_sources.setdefault(key, set()).add("project")

    merged: dict[str, typing.Any] = {}
    for key, sources in key_sources.items():
        if key in GLOBAL_PRIORITY_KEYS:
            if "global" in sources:
                merged[key] = normalized_global[key]
            else:
                merged[key] = normalized_project[key]
        else:
            if "project" in sources:
                merged[key] = normalized_project[key]
            else:
                merged[key] = normalized_global[key]
    return merged, key_sources


def make_config_warning_emitter(
    suppressed_warning_entries: frozenset[ConfigWarningEntry],
    warned_entries: set[ConfigWarningEntry],
) -> ConfigWarningEmitter:

    def emit(key: str, *, message: str) -> None:
        entry = (key, message)
        if entry in suppressed_warning_entries:
            return
        warned_entries.add(entry)
        pyfltr.warnings_.emit_warning(
            source="config",
            message=message,
            hint=f"設定ファイルの `{key}` を警告に示した形式へ修正するか削除し、再実行してください。",
        )

    return emit


def load_config(
    config_dir: pathlib.Path | None = None,
    *,
    global_config_path: pathlib.Path | None = None,
    for_subproject: bool = False,
    suppressed_warning_entries: frozenset[ConfigWarningEntry] = frozenset(),
) -> Config:
    config = create_default_config()
    base = config_dir or pathlib.Path.cwd()

    if global_config_path is None:
        global_config_path = default_global_config_path()
    global_data = read_global_config(global_config_path)

    pyproject_path = (base / "pyproject.toml").absolute()
    project_data: dict[str, typing.Any] = {}
    if pyproject_path.exists():
        text = read_config_text(pyproject_path)
        try:
            pyproject_doc = tomlkit.parse(text)
        except tomlkit.exceptions.TOMLKitError as e:
            raise ValueError(f"pyproject.tomlのTOML構文が不正です: {pyproject_path}: {e}") from e
        raw = pyproject_doc.get("tool", {})
        raw = raw.get("pyfltr", {}) if isinstance(raw, dict) else {}
        project_data = unwrap_tomlkit(raw) if isinstance(raw, dict) else {}

    for key in (pyfltr.config.overrides.OVERRIDES_KEY, pyfltr.config.overrides.ENV_FILE_KEY):
        if key in global_data or key.replace("-", "_") in global_data:
            global_data = {k: v for k, v in global_data.items() if k.replace("_", "-") != key}
            pyfltr.warnings_.emit_warning(
                source="config",
                message=f"グローバル設定の`{key}`は使えないため無視しました（`pyproject.toml`の`[tool.pyfltr]`に書く）",
                hint=f"グローバル設定ファイル（{global_config_path}）から`{key}`を削除してください。",
            )

    pending_override_warnings: list[tuple[str, str]] = []
    project_data = pyfltr.config.overrides.apply_overrides(
        project_data, pyproject_path.parent, lambda key, message: pending_override_warnings.append((key, message))
    )
    tool_pyfltr, key_sources = merge_global_and_project(global_data, project_data)
    effective_suppressed_entries = frozenset(
        entry for entry in suppressed_warning_entries if key_sources.get(entry[0]) == {"global"}
    )
    warned_entries: set[ConfigWarningEntry] = set()
    emit_config_warning = make_config_warning_emitter(effective_suppressed_entries, warned_entries)
    for key, message in dict.fromkeys(pending_override_warnings):
        emit_config_warning(key, message=message)

    priority_keys_overridden_by_global = sorted(
        key
        for key in GLOBAL_PRIORITY_KEYS
        if "project" in key_sources.get(key, set()) and "global" in key_sources.get(key, set())
    )
    if priority_keys_overridden_by_global:
        keys_str = ", ".join(priority_keys_overridden_by_global)
        pyfltr.warnings_.emit_warning(
            source="config",
            message=(f"archive/cache系のキーはglobal設定が優先されるため、project側の値は無視されます: {keys_str}"),
            hint="project側から該当キーを削除するか、global側の値を `pyfltr config set --global` で変更してください。",
        )

    apply_preset(config, tool_pyfltr, emit_config_warning)
    register_custom_commands(config, tool_pyfltr, emit_config_warning)
    apply_language_gate(config, tool_pyfltr, emit_config_warning)
    normalize_config_values(config, tool_pyfltr, emit_config_warning)
    validate_config(config, emit_config_warning)
    warned_global_only_entries = frozenset(entry for entry in warned_entries if key_sources.get(entry[0]) == {"global"})
    recompute_fast_aliases(config)
    if not for_subproject:
        warn_precommit_prek_conflict(config)

    return dataclasses.replace(config, warned_global_only_entries=warned_global_only_entries)


def apply_preset(
    config: Config,
    tool_pyfltr: dict[str, typing.Any],
    emit_config_warning: ConfigWarningEmitter,
) -> None:
    raw = tool_pyfltr.get("preset", "")
    if not isinstance(raw, str):
        emit_config_warning(
            "preset",
            message=(
                f"設定値 `preset` の型が不正です: 期待 文字列、実値 {japanese_type_label(raw)}。presetを適用せずに続行しました"
            ),
        )
        return
    preset = raw
    if preset == "":
        return
    if (preset_values := pyfltr.config.presets.get_preset(preset)) is not None:
        config.values.update(preset_values)
        config.values["preset"] = preset
        return
    if (removed_message := pyfltr.config.presets.get_removed_preset_message(preset)) is not None:
        emit_config_warning("preset", message=removed_message)
        return
    suggestions = close_matches(preset, pyfltr.config.presets.preset_names())
    message = f"`preset` の値が不正です: {preset!r}（許容値: {', '.join(pyfltr.config.presets.preset_names())}）"
    if suggestions:
        message = f"{message}。もしかして: {', '.join(suggestions)}"
    emit_config_warning("preset", message=f"{message}。presetを適用せずに続行しました")


def apply_language_gate(
    config: Config,
    tool_pyfltr: dict[str, typing.Any],
    emit_config_warning: ConfigWarningEmitter,
) -> None:
    user_keys = set(tool_pyfltr.keys())
    for category_key, commands in pyfltr.tools.LANGUAGE_CATEGORIES:
        raw = tool_pyfltr.get(category_key, False)
        if isinstance(raw, bool):
            enabled = raw
        else:
            emit_config_warning(
                category_key,
                message=(
                    f"設定値 `{category_key}` の型が不正です: 期待 真偽値、実値 {japanese_type_label(raw)}。"
                    "既定値 false（無効）として続行しました"
                ),
            )
            enabled = False
        if enabled:
            continue
        for cmd in commands:
            if cmd in user_keys:
                continue
            config.values[cmd] = False
