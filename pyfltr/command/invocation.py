"""解決済みコマンドに渡す引数の組み立て。"""

import os

import pyfltr.command.structured_output
import pyfltr.config.model
from pyfltr.tools import AUTO_ARGS, AUTO_VALUE_ARGS


def _strip_format_option(args: list[str]) -> list[str]:
    """引数列から `--format X` / `-f X` / `--format=X` を除去する （順序は保持）。

    textlintのfix実行時に使用する。`@textlint/fixer-formatter` はリンター側と
    異なるフォーマッタセットを持つため、ユーザーが共通argsに `--format compact` 等を
    指定していてもクラッシュしないように一律で除去する。compact文字列を特別扱いしないのは、
    `--format json` などの組み合わせに対しても安全に振る舞うため。
    """
    result: list[str] = []
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg in ("--format", "-f"):
            skip_next = True
            continue
        if arg.startswith("--format="):
            continue
        result.append(arg)
    return result


def _build_auto_args(command: str, config: pyfltr.config.model.Config, user_args: list[str]) -> list[str]:
    """自動引数を構築する。

    AUTO_ARGSで定義されたフラグがTrueの場合、対応する引数を返す。
    AUTO_VALUE_ARGSで定義された数値設定は正の値のときテンプレートを展開して返す。
    ユーザーが *-argsやCLI引数で既に同じ文字列（数値引数はフラグ名）を
    指定している場合はスキップする。
    """
    auto_entries = AUTO_ARGS.get(command, [])
    value_entries = AUTO_VALUE_ARGS.get(command, [])
    if not auto_entries and not value_entries:
        return []
    user_args_joined = " ".join(user_args)
    result: list[str] = []
    for flag_key, args in auto_entries:
        if not config.values.get(flag_key, False):
            continue
        for arg in args:
            if arg not in user_args_joined:
                result.append(arg)
    for value_key, template in value_entries:
        try:
            value = int(config.values.get(value_key, 0))
        except (TypeError, ValueError):
            continue
        if value <= 0:
            continue
        flag = template.split("=", maxsplit=1)[0]
        if flag in user_args_joined:
            continue
        result.append(template.format(value=value))
    return result


def expanduser_args(values: list[str]) -> list[str]:
    """各要素に `~` 展開を適用したリストを返す。

    対象キーは `{command}-path` / `{command}-args` / `{command}-extend-args` /
    `{command}-lint-args` / `{command}-fix-args` / `{command}-check-args` /
    `{command}-write-args` / `ruff-format-check-args` 経路に書かれた利用者ホームディレクトリ
    依存のパス（例: `~/dotfiles/.../tool.py`）。subprocess引数組み立て直前に展開する。
    展開対象キー一覧と適用タイミングのSSOTは
    `pyfltr.config.model.EXPAND_USER_KEY_SUFFIXES` を参照する。
    `config-files` / `targets` 等のglobパターンは意図しない展開を防ぐため対象外で、
    本ヘルパーを通さない。

    展開規則は次の2点。
    要素先頭が `~` または `~user` の場合は `os.path.expanduser` で展開する。
    要素内の最初の `=` 直後が `~` または `~user` の場合も展開する
    （`--config=~/cfg.toml` を `--config=<HOME>/cfg.toml` に展開する利便のため）。
    `os.path.expanduser` は先頭の `~` のみ展開するため、後者は要素を分割してから委譲する。
    """
    return [_expanduser_arg(value) for value in values]


def _expanduser_arg(value: str) -> str:
    expanded = os.path.expanduser(value)
    if expanded != value:
        return expanded
    sep_index = value.find("=")
    if sep_index < 0:
        return value
    prefix = value[: sep_index + 1]
    rest = value[sep_index + 1 :]
    expanded_rest = os.path.expanduser(rest)
    if expanded_rest == rest:
        return value
    return prefix + expanded_rest


def resolve_user_args(command: str, config: pyfltr.config.model.Config) -> list[str]:
    """`{command}-args` と `{command}-extend-args` を `~` 展開した上で連結して返す。

    `{command}-extend-args` は既定値の `{command}-args` を保ったまま末尾へ要素を追加する用途。
    両キーとも未設定なら空リストを返す。`build_invocation_argv` と `two_step` 経路の双方が
    本ヘルパーを経由することで、結合順序・展開タイミングのSSOTを確保する。
    """
    args = expanduser_args(list(pyfltr.config.model.command_setting(config.values, command, "args", [])))
    extend_args = expanduser_args(list(pyfltr.config.model.command_setting(config.values, command, "extend-args", [])))
    return args + extend_args


def build_invocation_argv(
    command: str,
    config: pyfltr.config.model.Config,
    commandline_prefix: list[str],
    additional_args: list[str],
    *,
    fix_stage: bool,
) -> list[str]:
    """対象ファイル抜きの起動argvを構築する共通ヘルパー。

    通常段では `[prefix] + auto_args + {command}-args + {command}-extend-args +
    {command}-lint-args + additional_args` に構造化出力引数を適用した結果を返す。
    fix段では `{command}-fix-args` を結合する。
    fix-args未定義のコマンドではfix_stage=Trueでも通常段と同じargvを返す。

    `{command}-args` / `{command}-extend-args` / `{command}-lint-args` /
    `{command}-fix-args` の各要素は `expanduser_args` で `~` 展開してから結合する。
    利用者ホームディレクトリ依存のパスを設定値として指定できるようにするための処置。
    `commandline_prefix` 側（`{command}-path` 由来）の展開は `build_commandline` で済ませている。

    textlintのfix段は `execute_textlint_fix` のStep1と同じ規則
    （`--format` ペアを除去したargs + fix-args + additional_args + `--no-cache`、
    auto_args / 構造化出力引数なし）を適用する。
    実行本体（`_prepare_execution_params` / `execute_textlint_fix`）と
    `command-info` 表示の双方から本ヘルパーへ集約することで、組み立て規則の重複定義を避ける。
    """
    user_args: list[str] = resolve_user_args(command, config)
    extra: list[str] = list(additional_args)

    # textlintのfix Step1はfixer-formatterがcompact系をサポートしないため、
    # ユーザー指定の `--format` ペアを一律で除去したうえでfix-argsを結合する特殊経路。
    # auto_args・構造化出力引数も適用しない（fixer出力の解析は本ステップでは行わないため）。
    # 末尾の`--no-cache`は修正段だけtextlintのキャッシュを無効化する。textlint 15.8.0はキャッシュヒット時に
    # 修正後の本文を持たない結果を修正処理へ渡して例外終了するため、利用者が通常lint向けに`--cache`を
    # 設定していても修正段では使わない。後勝ちで適用されるよう利用者引数の結合後に置き、通常段の引数は変えない。
    if fix_stage and command == "textlint":
        fix_args: list[str] = expanduser_args(list(pyfltr.config.model.command_setting(config.values, command, "fix-args", [])))
        return [
            *commandline_prefix,
            *_strip_format_option(user_args),
            *fix_args,
            *extra,
            "--no-cache",
        ]

    fix_args_value: list[str] | None = None
    if fix_stage:
        raw = pyfltr.config.model.command_setting(config.values, command, "fix-args", None)
        fix_args_value = expanduser_args(list(raw)) if raw is not None else None

    auto_args = _build_auto_args(command, config, user_args + extra)
    commandline: list[str] = [*commandline_prefix, *auto_args, *user_args]
    if fix_args_value is not None:
        commandline.extend(fix_args_value)
    else:
        commandline.extend(expanduser_args(list(pyfltr.config.model.command_setting(config.values, command, "lint-args", []))))
    commandline.extend(extra)
    structured_spec = pyfltr.command.structured_output.get_structured_output_spec(command, config)
    if structured_spec is not None and not (structured_spec.lint_only and fix_args_value is not None):
        commandline = pyfltr.command.structured_output.apply_structured_output(commandline, structured_spec)
    return commandline
