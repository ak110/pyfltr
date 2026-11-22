import dataclasses
import difflib
import pathlib
import re
import typing

import pyfltr.parsing.common
import pyfltr.tool_types
import pyfltr.tools
import pyfltr.warnings_
from pyfltr.config.model import DEFAULT_CONFIG, SEVERITY_VALUES, Config, ConfigWarningEmitter
from pyfltr.config.selection import build_fast_alias


def register_custom_commands(
    config: Config,
    tool_pyfltr: dict[str, typing.Any],
    emit_config_warning: ConfigWarningEmitter,
) -> None:
    custom_commands = tool_pyfltr.get("custom-commands", {})
    if not isinstance(custom_commands, dict):
        emit_config_warning(
            "custom-commands",
            message="`custom-commands` はテーブルで指定してください: 例 [tool.pyfltr.custom-commands.svelte-check]",
        )
        return
    for name, definition in custom_commands.items():
        name = name.replace("_", "-")
        register_custom_command(config, name, definition, emit_config_warning)


def normalize_config_values(
    config: Config,
    tool_pyfltr: dict[str, typing.Any],
    emit_config_warning: ConfigWarningEmitter,
) -> None:
    skip_keys = ("preset", "custom-commands", *(key for key, _ in pyfltr.tools.LANGUAGE_CATEGORIES))
    targets_overrides: dict[str, str | list[str]] = {}
    extend_targets_map: dict[str, str | list[str]] = {}

    for key, value in tool_pyfltr.items():
        if key in skip_keys:
            continue
        removed_owner = extract_removed_command(key)
        if removed_owner is not None:
            emit_config_warning(
                key,
                message=(
                    f'"{key}" は v3.0.0 で削除されたツール "{removed_owner}" 向けの設定である。'
                    "5 ツール (pyupgrade / autoflake / isort / black / pflake8) は ruff への統合により削除された。"
                    "該当設定をすべて pyproject.toml から除去すること"
                ),
            )
            continue
        if key.endswith("-slot-ticket") and key.removesuffix("-slot-ticket") in config.commands:
            emit_config_warning(
                key,
                message=(
                    f'"{key}" は撤去された設定である（検証の実行枠への協調参加の廃止）。'
                    "設定は無視して続行した。pyproject.toml から除去すること"
                ),
            )
            continue
        if key in ("pytest-fast-targets", "pytest-always-targets"):
            validated = validate_targets_value(key, value, emit_config_warning)
            if validated is not None:
                config.values[key] = validated
            continue
        if key.endswith("-exclude"):
            cmd_name = key.removesuffix("-exclude")
            if cmd_name in config.commands:
                if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                    emit_config_warning(
                        key,
                        message=f'`{key}` はstr型のリストで指定してください: 例 ["vendor", "gen_*.py"]。このキーは無視しました',
                    )
                    continue
                config.values[key] = value
                continue
        if key.endswith("-extend-targets"):
            cmd_name = key.removesuffix("-extend-targets")
            if cmd_name in config.commands:
                validated = validate_targets_value(key, value, emit_config_warning)
                if validated is not None:
                    config.values[key] = validated
                    extend_targets_map[cmd_name] = validated
                continue
        if key.endswith("-extend-args"):
            cmd_name = key.removesuffix("-extend-args")
            if cmd_name in config.commands:
                if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                    emit_config_warning(
                        key,
                        message=f'`{key}` はstr型のリストで指定してください: 例 ["--exclude=foo"]。このキーは無視しました',
                    )
                    continue
                config.values[key] = value
                continue
        if key.endswith("-targets"):
            cmd_name = key.removesuffix("-targets")
            if cmd_name in config.commands:
                validated = validate_targets_value(key, value, emit_config_warning)
                if validated is not None:
                    config.values[key] = validated
                    targets_overrides[cmd_name] = validated
                continue
        if key not in config.values:
            emit_config_warning(
                key,
                message=format_unknown_key_message(key, config.values.keys()),
            )
            continue
        if not isinstance(value, type(config.values[key])):
            expected_label = japanese_type_label(config.values[key])
            actual_label = japanese_type_label(value)
            emit_config_warning(
                key,
                message=(
                    f"設定値 `{key}` の型が不正です: 期待 {expected_label}、実値 {actual_label}。"
                    "このキーは無視し、既定値で続行しました"
                ),
            )
            continue
        config.values[key] = value

    for cmd_name, new_targets in targets_overrides.items():
        config.commands[cmd_name] = dataclasses.replace(config.commands[cmd_name], targets=new_targets)

    for cmd_name, extra in extend_targets_map.items():
        existing = config.commands[cmd_name].target_globs()
        if isinstance(extra, str):
            existing.append(extra)
        else:
            existing.extend(extra)
        config.commands[cmd_name] = dataclasses.replace(config.commands[cmd_name], targets=existing)


def validate_config(config: Config, emit_config_warning: ConfigWarningEmitter) -> None:
    _global_runner_specs: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("python-runner", pyfltr.tools.PYTHON_RUNNERS),
        ("js-runner", pyfltr.tools.JS_RUNNERS),
        ("bin-runner", pyfltr.tools.BIN_RUNNERS),
    )
    for runner_key, allowed in _global_runner_specs:
        runner_value = config.values[runner_key]
        if runner_value not in allowed:
            emit_config_warning(
                runner_key,
                message=(
                    f"`{runner_key}` の値が不正です: {runner_value!r}（許容値: {', '.join(allowed)}）。"
                    f"既定値 {DEFAULT_CONFIG[runner_key]!r} で続行しました"
                ),
            )
            config.values[runner_key] = DEFAULT_CONFIG[runner_key]

    _global_runner_keys = frozenset(key for key, _ in _global_runner_specs)
    for key, value in list(config.values.items()):
        if not key.endswith("-runner") or key in _global_runner_keys:
            continue
        if value not in pyfltr.tools.COMMAND_RUNNERS:
            fallback = DEFAULT_CONFIG.get(key, "direct")
            emit_config_warning(
                key,
                message=(
                    f"`{key}` の値が不正です: {value!r}（許容値: {', '.join(pyfltr.tools.COMMAND_RUNNERS)}）。"
                    f"既定値 {fallback!r} で続行しました"
                ),
            )
            config.values[key] = fallback

    for key, value in list(config.values.items()):
        if not key.endswith("-severity"):
            continue
        if value not in SEVERITY_VALUES:
            emit_config_warning(
                key,
                message=(
                    f"`{key}` の値が不正です: {value!r}（許容値: {', '.join(SEVERITY_VALUES)}）。既定値 'error' で続行しました"
                ),
            )
            config.values[key] = "error"

    for key, value in list(config.values.items()):
        if not key.endswith("-hints"):
            continue
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            emit_config_warning(
                key,
                message=(
                    f'`{key}` は文字列のリストで指定してください: 例 ["注意1", "注意2"]、実値 {value!r}。このキーは無視しました'
                ),
            )
            config.values[key] = []


def recompute_fast_aliases(config: Config) -> None:
    config.values["aliases"]["fast"] = build_fast_alias(config)


def warn_config_files(config: Config, base: pathlib.Path, commands: typing.Iterable[str]) -> None:
    for command in commands:
        info = config.commands.get(command)
        if info is None or not info.config_files:
            continue
        if any(list(base.glob(pattern)) for pattern in info.config_files):
            continue
        candidates = ", ".join(info.config_files)
        pyfltr.warnings_.emit_warning(
            source="config",
            message=(
                f"{command} が有効化されていますが、設定ファイルが見つかりません: {candidates}。"
                "ツールは設定ファイル無しで起動するため、既定の設定で検査されるか、設定不足で失敗します"
            ),
            hint=f"候補のいずれかを作成するか、不要なら `{command} = false` で無効化してください。",
        )


def warn_precommit_prek_conflict(config: Config) -> None:
    if config["pre-commit"] and config["prek"]:
        pyfltr.warnings_.emit_warning(
            source="config",
            message=("pre-commit と prek が両方有効化されています。同一の.pre-commit-config.yamlのフックが二重実行されます。"),
            hint="pyproject.tomlの [tool.pyfltr] で pre-commit または prek のいずれか一方を false にしてください。",
        )


def register_custom_command(
    config: Config,
    name: str,
    definition: dict[str, typing.Any],
    emit_config_warning: ConfigWarningEmitter,
) -> None:

    def _skip_registration(message: str) -> None:
        emit_config_warning("custom-commands", message=f"{message}。このカスタムコマンドの登録をスキップしました")

    if name in pyfltr.tools.BUILTIN_COMMANDS:
        emit_config_warning(
            "custom-commands",
            message=(
                f"カスタムコマンド `{name}` がビルトインコマンドと衝突するため、登録をスキップしました。"
                f"別名へ変更するか、ビルトインの `{name}-args` などでビルトイン側の設定を上書きしてください"
            ),
        )
        return

    cmd_type = definition.get("type")
    if cmd_type not in ("formatter", "linter", "tester"):
        _skip_registration(
            f"カスタムコマンド `{name}` の `type` が不正です: {cmd_type!r}（許容値: formatter, linter, tester）",
        )
        return

    path = definition.get("path", name)
    if not isinstance(path, str):
        _skip_registration(
            f"カスタムコマンド `{name}` の `path` は文字列で指定してください",
        )
        return

    args = definition.get("args", [])
    if not isinstance(args, list):
        _skip_registration(
            f"カスタムコマンド `{name}` の `args` はリストで指定してください",
        )
        return

    extend_args = definition.get("extend-args", definition.get("extend_args", []))
    if not isinstance(extend_args, list) or not all(isinstance(item, str) for item in extend_args):
        _skip_registration(
            f"カスタムコマンド `{name}` の `extend-args` は文字列のリストで指定してください",
        )
        return

    fix_args = definition.get("fix-args", definition.get("fix_args"))
    if fix_args is not None and not isinstance(fix_args, list):
        _skip_registration(
            f"カスタムコマンド `{name}` の `fix-args` はリストで指定してください",
        )
        return

    raw_targets: typing.Any = definition.get("targets", "*.py")
    targets: str | list[str]
    if isinstance(raw_targets, str):
        targets = raw_targets
    elif isinstance(raw_targets, list) and all(isinstance(item, str) for item in raw_targets):
        targets = [str(item) for item in raw_targets]
    else:
        _skip_registration(
            f"カスタムコマンド `{name}` の `targets` は文字列または文字列のリストで指定してください",
        )
        return

    error_pattern = definition.get("error-pattern", definition.get("error_pattern"))
    if error_pattern is not None:
        if not isinstance(error_pattern, str):
            _skip_registration(
                f"カスタムコマンド `{name}` の `error-pattern` は文字列で指定してください",
            )
            return
        try:
            compiled = re.compile(error_pattern)
        except re.error as e:
            _skip_registration(
                f"カスタムコマンド `{name}` の `error-pattern` が不正な正規表現です: {e}。"
                "error-patternの該当位置を修正してください",
            )
            return
        missing_group = next(
            (g for g in pyfltr.parsing.common.REQUIRED_ERROR_PATTERN_GROUPS if g not in compiled.groupindex), None
        )
        if missing_group is not None:
            _skip_registration(
                f"カスタムコマンド `{name}` の `error-pattern` に `{missing_group}` 名前付きグループが必要です",
            )
            return

    raw_config_files: typing.Any = definition.get("config-files", definition.get("config_files", []))
    if not isinstance(raw_config_files, list) or not all(isinstance(item, str) for item in raw_config_files):
        _skip_registration(
            f"カスタムコマンド `{name}` の `config-files` は文字列のリストで指定してください",
        )
        return
    config_files: list[str] = [str(item) for item in raw_config_files]

    fast = definition.get("fast", False)
    if not isinstance(fast, bool):
        _skip_registration(
            f"カスタムコマンド `{name}` の `fast` は真偽値で指定してください",
        )
        return

    pass_filenames = definition.get("pass-filenames", definition.get("pass_filenames", True))
    if not isinstance(pass_filenames, bool):
        _skip_registration(
            f"カスタムコマンド `{name}` の `pass-filenames` は真偽値で指定してください",
        )
        return

    raw_severity: typing.Any = definition.get("severity", "error")
    if raw_severity not in SEVERITY_VALUES:
        _skip_registration(
            (
                f"カスタムコマンド `{name}` の `severity` の値が不正です: "
                f"{raw_severity!r}（許容値: {', '.join(SEVERITY_VALUES)}）"
            ),
        )
        return
    severity: str = str(raw_severity)

    raw_hints: typing.Any = definition.get("hints", [])
    if not isinstance(raw_hints, list) or not all(isinstance(item, str) for item in raw_hints):
        _skip_registration(
            f"カスタムコマンド `{name}` の `hints` は文字列のリストで指定してください",
        )
        return
    hints: list[str] = [str(item) for item in raw_hints]

    config.commands[name] = pyfltr.tool_types.CommandInfo(
        type=cmd_type,
        builtin=False,
        targets=targets,
        error_pattern=error_pattern,
        config_files=config_files,
    )
    config.command_names.append(name)

    config.values[name] = True
    config.values[f"{name}-path"] = path
    config.values[f"{name}-args"] = args
    config.values[f"{name}-extend-args"] = [str(item) for item in extend_args]
    config.values[f"{name}-fast"] = fast
    config.values[f"{name}-pass-filenames"] = pass_filenames
    config.values[f"{name}-severity"] = severity
    config.values[f"{name}-hints"] = hints
    config.values[f"{name}-subproject-aware"] = config.commands[name].subproject_aware
    if fix_args is not None:
        config.values[f"{name}-fix-args"] = fix_args


def extract_removed_command(key: str) -> str | None:
    if key in pyfltr.tools.REMOVED_COMMANDS:
        return key
    for command in pyfltr.tools.REMOVED_COMMANDS:
        if key.startswith(f"{command}-"):
            return command
    return None


def validate_targets_value(
    key: str,
    value: typing.Any,
    emit_config_warning: ConfigWarningEmitter,
) -> str | list[str] | None:
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return [str(item) for item in value]
    emit_config_warning(
        key,
        message=f"`{key}` は文字列または文字列のリストで指定してください。このキーは無視し、既定の対象で続行しました",
    )
    return None


_JAPANESE_TYPE_LABELS: dict[type, str] = {
    bool: "真偽値",
    int: "整数",
    float: "数値",
    str: "文字列",
    list: "リスト",
    tuple: "リスト",
    dict: "テーブル",
}


"""Python型 → エラーメッセージ向け日本語ラベル。

`<class 'bool'>` のような生表示を避け、利用者が型として識別できる語へ揃える。
未登録型は `type(value).__name__` のフォールバックを使う。
"""


def japanese_type_label(value: typing.Any) -> str:
    target_type = value if isinstance(value, type) else type(value)
    return _JAPANESE_TYPE_LABELS.get(target_type, target_type.__name__)


def close_matches(key: str, candidates: typing.Iterable[str]) -> list[str]:
    return difflib.get_close_matches(key, list(candidates), n=3, cutoff=0.6)


def format_unknown_key_message(key: str, candidates: typing.Iterable[str]) -> str:
    suggestions = close_matches(key, candidates)
    parts = [f"設定キー `{key}` は認識できません"]
    if suggestions:
        parts.append(f"もしかして: {', '.join(suggestions)}")
    parts.append("有効なキー一覧は `pyfltr config list --all` で確認できます")
    return "。".join(parts)
