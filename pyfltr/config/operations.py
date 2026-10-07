"""CLIとMCPで共有する設定操作の要求と結果。"""

import dataclasses
import pathlib
import typing

import pyfltr.config.config
import pyfltr.config.editing
import pyfltr.config.model
import pyfltr.config.validation
import pyfltr.warnings_


@dataclasses.dataclass(frozen=True)
class ConfigRequest:
    """設定操作の入力。表示する操作名は呼び出し元が渡す。"""

    action: str
    path: pathlib.Path
    key: str | None = None
    value: str | None = None
    use_global: bool = False
    include_defaults: bool = False
    global_option: str = "`--global`"


@dataclasses.dataclass
class ConfigResult:
    """設定操作の結果と、公開入口が表示へ変換する失敗情報。"""

    value: typing.Any = None
    is_default: bool = False
    values: dict[str, typing.Any] = dataclasses.field(default_factory=dict)
    existed: bool = False
    path_existed: bool = False
    warnings: list[str] = dataclasses.field(default_factory=list)
    error: str | None = None
    error_stage: str = "read"
    io_error: bool = False


def execute(request: ConfigRequest) -> ConfigResult:
    """設定の検証・変換・読み書きを実行し、想定内のエラーを結果へ含める。"""
    result = ConfigResult()
    try:
        result.path_existed = request.path.exists()
        key = request.key
        if request.action == "set" and not request.use_global and not result.path_existed:
            result.error_stage = "missing"
            raise ValueError(pyfltr.config.config.format_project_config_missing(request.path, use_global=request.global_option))
        if request.action != "list" and request.action != "get" and key not in pyfltr.config.model.DEFAULT_CONFIG:
            result.error_stage = "unknown"
            raise ValueError(
                pyfltr.config.validation.format_unknown_key_message(str(key), pyfltr.config.model.DEFAULT_CONFIG.keys())
            )
        if request.action == "get":
            values = pyfltr.config.editing.read_config_values(request.path)
            if key in values:
                result.value = values[key]
            elif key in pyfltr.config.model.DEFAULT_CONFIG:
                result.value = pyfltr.config.model.DEFAULT_CONFIG[key]
                result.is_default = True
            else:
                result.error_stage = "unknown"
                raise ValueError(
                    pyfltr.config.validation.format_unknown_key_message(str(key), pyfltr.config.model.DEFAULT_CONFIG.keys())
                )
        elif request.action == "set":
            result.error_stage = "value"
            result.value = pyfltr.config.editing.parse_config_value(typing.cast(str, key), typing.cast(str, request.value))
            if key in pyfltr.config.model.GLOBAL_PRIORITY_KEYS and not request.use_global:
                pyfltr.warnings_.emit_warning(
                    source="config",
                    message=(
                        f"{key} はarchive/cache系のキーです。マシン共通設定として"
                        f" {request.global_option.strip('`')} での設定を推奨します（global側があればglobal優先になります）。"
                    ),
                )
            elif key not in pyfltr.config.model.GLOBAL_PRIORITY_KEYS and request.use_global:
                pyfltr.warnings_.emit_warning(
                    source="config",
                    message=(
                        f"{key} は通常キーのためproject側のpyproject.tomlが優先されます。"
                        " globalに書いてもproject側に同じキーがあれば上書きされます。"
                    ),
                )
            result.error_stage = "write"
            pyfltr.config.editing.set_config_value(
                request.path,
                typing.cast(str, key),
                result.value,
                create_if_missing=request.use_global,
            )
        elif request.action == "delete":
            result.existed = pyfltr.config.editing.delete_config_value(request.path, typing.cast(str, key))
        elif request.action == "list":
            values = pyfltr.config.editing.read_config_values(request.path)
            result.values = (
                {
                    name: {"value": values.get(name, default), "default": name not in values}
                    for name, default in sorted(pyfltr.config.model.DEFAULT_CONFIG.items())
                }
                if request.include_defaults
                else values
            )
        else:
            raise AssertionError(f"未知のconfig action: {request.action!r}")
    except (ValueError, OSError) as error:
        result.error = str(error)
        result.io_error = isinstance(error, OSError)
    result.warnings = [pyfltr.warnings_.format_warning_text(entry) for entry in pyfltr.warnings_.collected_warnings()]
    return result
