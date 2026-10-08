"""設定値・カスタムコマンドの正規化と検証。"""

import dataclasses
import difflib
import pathlib
import re
import typing

import pyfltr.tools
import pyfltr.warnings_
from pyfltr.config.model import DEFAULT_CONFIG, SEVERITY_VALUES, Config, ConfigWarningEmitter
from pyfltr.config.selection import build_fast_alias


def register_custom_commands(
    config: Config,
    tool_pyfltr: dict[str, typing.Any],
    emit_config_warning: ConfigWarningEmitter,
) -> None:
    """custom-commandsエントリを読み取り、各カスタムコマンドをconfigに登録する。

    `custom-commands`配下がテーブル以外の場合は警告してカスタムコマンド登録処理全体を
    スキップする。
    """
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
    """プリセット・言語カテゴリ以外の設定を適用し、targets/extend-targetsを反映する。

    プリセットと重複するキーは上書きされる。
    未知キー・型不一致・excludeのリスト型不一致・targetsの型不一致・削除済みコマンド向け
    キーは警告して既定値を維持する。由来（global / project）に依らず同じ警告経路を
    適用する（複数バージョン混在時の停止回避が目的）。
    """
    skip_keys = ("preset", "custom-commands", *(key for key, _ in pyfltr.tools.LANGUAGE_CATEGORIES))
    targets_overrides: dict[str, str | list[str]] = {}
    extend_targets_map: dict[str, str | list[str]] = {}

    for key, value in tool_pyfltr.items():
        if key in skip_keys:
            continue  # 別途処理済み
        # v3.0.0で削除されたツール名に紐づく設定キーを検出したら移行案内を表示する。
        # "pyupgrade" / "pyupgrade-path" / "pyupgrade-args" / "pyupgrade-fast"などを網羅する。
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
        # pytest-fast-targets・pytest-always-targetsは`-targets`接尾辞を持つがコマンド別targetsではないため先に扱う。
        if key in ("pytest-fast-targets", "pytest-always-targets"):
            validated = validate_targets_value(key, value, emit_config_warning)
            if validated is not None:
                config.values[key] = validated
            continue
        # {command}-excludeの検出
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
        # {command}-extend-targetsの検出（長いサフィックスを先に判定）
        if key.endswith("-extend-targets"):
            cmd_name = key.removesuffix("-extend-targets")
            if cmd_name in config.commands:
                validated = validate_targets_value(key, value, emit_config_warning)
                if validated is not None:
                    config.values[key] = validated
                    extend_targets_map[cmd_name] = validated
                continue
        # {command}-extend-argsの検出。サフィックス`-args`より長いため、後段の
        # `key not in config.values`分岐や`-args`相当の汎用一致判定より先に処理する。
        # `{command}-args`の末尾へ結合する追加引数で、既定値を保ったまま要素を足す用途。
        # 値はstr型のリストとし、不正な場合は警告して既定値（空リスト）を維持する。
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
        # {command}-targetsの検出
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
        if not isinstance(value, type(config.values[key])):  # 簡易チェック
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

    # targetsの完全上書き
    for cmd_name, new_targets in targets_overrides.items():
        config.commands[cmd_name] = dataclasses.replace(config.commands[cmd_name], targets=new_targets)

    # extend-targetsの追加（targets上書き後に適用）
    for cmd_name, extra in extend_targets_map.items():
        existing = config.commands[cmd_name].target_globs()
        if isinstance(extra, str):
            existing.append(extra)
        else:
            existing.extend(extra)
        config.commands[cmd_name] = dataclasses.replace(config.commands[cmd_name], targets=existing)


def validate_config(config: Config, emit_config_warning: ConfigWarningEmitter) -> None:
    """Runner / severity / hintsのバリデーションを行う。

    値が不正な場合は警告を発行し、対象のキーの値を既定値（`DEFAULT_CONFIG`）に
    巻き戻して処理を続行する。複数バージョン混在時に新しい値が旧バージョンへ
    波及してもパイプライン全体は停止しない方針。
    """
    # グローバルrunner設定（python-runner / js-runner / bin-runner）の値バリデーション。
    # カテゴリごとに許容値が異なるため、共通dispatcher構造で1箇所に集約する。
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

    # per-tool {command}-runnerの値バリデーション。
    # 対称12値のいずれかを許容する。カテゴリ横断の組み合わせ（例: Python系ツールに`pnpm`を指定）は
    # 拒否しない方針（実装簡潔さを優先し、無意味な組み合わせは実行時の解決ロジックがエラー終了する）。
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

    # per-tool {command}-severityの値バリデーション。
    # ビルトイン分は既定値 "error" が登録済みでも、利用者が pyproject.toml で
    # 別値を書いた場合は本ループで検出する。カスタムコマンド側は
    # `register_custom_command` で登録時に検証済みのため、ここでは値のみ確認する。
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

    # per-tool {command}-hintsの要素型バリデーション。
    # 上位の汎用バリデーション（list型一致）はパスするが、要素がstrでなければ
    # JSONL出力時に文字列前提のレコード組み立てが失敗するため、ここで明示的に
    # 文字列リストであることを確認する。
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
    """per-command fastフラグからfastエイリアスを再計算する。"""
    config.values["aliases"]["fast"] = build_fast_alias(config)


def warn_config_files(config: Config, base: pathlib.Path, commands: typing.Iterable[str]) -> None:
    """今回の実行対象で`pyfltr.tools.CommandInfo.config_files`を満たさないものを警告する。

    `commands`には有効判定（`is_command_enabled_anywhere`）とエイリアス展開を経た実行対象を渡す。
    設定上は有効でも今回選択していないコマンドを判定対象から外し、限定実行で無関係な警告を返さないため、
    設定値だけで判定せず実行対象の確定後に呼び出す。
    設定ファイルの探索起点`base`はモノレポでも起点cwdとする。
    """
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
    """pre-commitとprekが同時に有効化されている場合に警告する。

    双方が同一の.pre-commit-config.yamlのフックを実行するため、同時有効化は
    二重実行を招く。実行自体は妨げず警告のみとする。
    """
    if config["pre-commit"] and config["prek"]:
        pyfltr.warnings_.emit_warning(
            source="config",
            message=(
                "pre-commit と prek が両方有効化されています。"
                "同一の.pre-commit-config.yamlのフックが二重実行されるため、いずれか一方のみを有効化してください。"
            ),
        )


def register_custom_command(
    config: Config,
    name: str,
    definition: dict[str, typing.Any],
    emit_config_warning: ConfigWarningEmitter,
) -> None:
    """カスタムコマンドをConfigに登録する。

    定義のいずれかが不正と判定された場合は警告を発行して対象のカスタムコマンドの登録自体を
    スキップする。値の部分採用（一部のみ反映）は行わない。
    検証はすべての項目を `config.commands` / `config.values` 更新前に完了させる。
    """

    def _skip_registration(message: str) -> None:
        emit_config_warning("custom-commands", message=f"{message}。このカスタムコマンドの登録をスキップしました")

    # 名前衝突チェック
    if name in pyfltr.tools.BUILTIN_COMMANDS:
        emit_config_warning(
            "custom-commands",
            message=(
                f"カスタムコマンド `{name}` がビルトインコマンドと衝突するため、登録をスキップしました。"
                f"別名へ変更するか、ビルトインの `{name}-args` などでビルトイン側の設定を上書きしてください"
            ),
        )
        return

    # type (必須)
    cmd_type = definition.get("type")
    if cmd_type not in ("formatter", "linter", "tester"):
        _skip_registration(
            f"カスタムコマンド `{name}` の `type` が不正です: {cmd_type!r}（許容値: formatter, linter, tester）",
        )
        return

    # path (省略時はコマンド名)
    path = definition.get("path", name)
    if not isinstance(path, str):
        _skip_registration(
            f"カスタムコマンド `{name}` の `path` は文字列で指定してください",
        )
        return

    # args (省略時は空リスト)
    args = definition.get("args", [])
    if not isinstance(args, list):
        _skip_registration(
            f"カスタムコマンド `{name}` の `args` はリストで指定してください",
        )
        return

    # extend-args（省略可。省略時は空リスト扱い）。
    # 既定値の`args`を保ったまま末尾へ追加する引数。ビルトインコマンドと同じ意味づけ。
    extend_args = definition.get("extend-args", definition.get("extend_args", []))
    if not isinstance(extend_args, list) or not all(isinstance(item, str) for item in extend_args):
        _skip_registration(
            f"カスタムコマンド `{name}` の `extend-args` は文字列のリストで指定してください",
        )
        return

    # fix-args（省略可。省略時はfixモード非対応として扱う）
    fix_args = definition.get("fix-args", definition.get("fix_args"))
    if fix_args is not None and not isinstance(fix_args, list):
        _skip_registration(
            f"カスタムコマンド `{name}` の `fix-args` はリストで指定してください",
        )
        return

    # targets（省略時は "*.py"。strまたはlist[str]）
    raw_targets: typing.Any = definition.get("targets", "*.py")
    targets: str | list[str]
    if isinstance(raw_targets, str):
        targets = raw_targets
    elif isinstance(raw_targets, list) and all(isinstance(item, str) for item in raw_targets):
        # raw_targetsはtyping.Any経由のためlist(raw_targets)の要素型が縮まらない。
        # 上記isinstanceで要素がstrであることを検証済みなので、明示的にstr化して
        # list[str]を構築する。
        targets = [str(item) for item in raw_targets]
    else:
        _skip_registration(
            f"カスタムコマンド `{name}` の `targets` は文字列または文字列のリストで指定してください",
        )
        return

    # error-pattern（省略可）
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
                f"カスタムコマンド `{name}` の `error-pattern` が不正な正規表現です: {e}",
            )
            return
        missing_group = next((g for g in ("file", "line", "message") if g not in compiled.groupindex), None)
        if missing_group is not None:
            _skip_registration(
                f"カスタムコマンド `{name}` の `error-pattern` に `{missing_group}` 名前付きグループが必要です",
            )
            return

    # config-files（省略可。設定ファイル候補のglobパターン）
    raw_config_files: typing.Any = definition.get("config-files", definition.get("config_files", []))
    if not isinstance(raw_config_files, list) or not all(isinstance(item, str) for item in raw_config_files):
        _skip_registration(
            f"カスタムコマンド `{name}` の `config-files` は文字列のリストで指定してください",
        )
        return
    config_files: list[str] = [str(item) for item in raw_config_files]

    # fast（省略時はFalse）
    fast = definition.get("fast", False)
    if not isinstance(fast, bool):
        _skip_registration(
            f"カスタムコマンド `{name}` の `fast` は真偽値で指定してください",
        )
        return

    # pass-filenames（省略時はTrue）
    pass_filenames = definition.get("pass-filenames", definition.get("pass_filenames", True))
    if not isinstance(pass_filenames, bool):
        _skip_registration(
            f"カスタムコマンド `{name}` の `pass-filenames` は真偽値で指定してください",
        )
        return

    # severity（省略時は "error"）。許容値以外は警告して登録スキップ。
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

    # hints（省略時は空リスト。要素はstr）。
    raw_hints: typing.Any = definition.get("hints", [])
    if not isinstance(raw_hints, list) or not all(isinstance(item, str) for item in raw_hints):
        _skip_registration(
            f"カスタムコマンド `{name}` の `hints` は文字列のリストで指定してください",
        )
        return
    hints: list[str] = [str(item) for item in raw_hints]

    # 全検証通過。pyfltr.tools.CommandInfo・values辞書を一括登録する。
    config.commands[name] = pyfltr.tools.CommandInfo(
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
    # ビルトインコマンドは`register_command_subproject_aware_defaults`が既定値を登録する。
    # カスタムコマンドは登録時点でしか既定値を用意できないため、ここで併せて登録する。
    # 登録しないと利用者が`{name}-subproject-aware`を指定したとき未知キーとして警告される。
    config.values[f"{name}-subproject-aware"] = config.commands[name].subproject_aware
    # fix-argsは定義されている場合のみ登録する（キーの有無でfix対応可否を判別）
    if fix_args is not None:
        config.values[f"{name}-fix-args"] = fix_args


def extract_removed_command(key: str) -> str | None:
    """設定キーが削除コマンド宛なら該当コマンド名を返す、そうでなければNone。

    `"pyupgrade"`のようなbare keyと、`"pyupgrade-path"` / `"pyupgrade-args"` /
    `"pyupgrade-fast"`などの派生キーの双方を検出する。
    """
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
    """Targets / extend-targets の値をバリデーション。

    値が不正な場合は警告を発行し、`None`を返す。呼び出し側は`None`を受け取ったら
    対象のキーの反映をスキップして既定値（`pyfltr.tools.CommandInfo.targets`）を維持する。
    """
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return [str(item) for item in value]
    emit_config_warning(
        key,
        message=f"`{key}` は文字列または文字列のリストで指定してください。このキーは無視し、既定の対象で続行しました",
    )
    return None


# `close_matches`/`japanese_type_label`/`format_unknown_key_message` はconfig.py内で
# 2箇所以上から再利用するため、共通モジュール新設は行わずプライベートヘルパーとして集約する。

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
    """値または型から日本語ラベルを返す。

    `value` には実値・型オブジェクトのいずれでも渡せる（呼び分けを揃えるため）。
    未登録型は型名（`type.__name__`）をそのまま返す。
    """
    target_type = value if isinstance(value, type) else type(value)
    return _JAPANESE_TYPE_LABELS.get(target_type, target_type.__name__)


def close_matches(key: str, candidates: typing.Iterable[str]) -> list[str]:
    """`key` に近いキー名候補をdifflib由来で最大3件返す。

    呼び出し側は候補が空のときにサジェスト文を出力しない判断を行う。
    """
    return difflib.get_close_matches(key, list(candidates), n=3, cutoff=0.6)


def format_unknown_key_message(key: str, candidates: typing.Iterable[str]) -> str:
    """未知設定キー検出時の文面を組み立てる。

    候補があれば「もしかして: ...」を併記し、必ず全キー一覧確認手段を案内する。
    `config.py` / `cli/config_subcmd.py`の双方から再利用するためpublic名で公開する。
    """
    suggestions = close_matches(key, candidates)
    parts = [f"設定キー `{key}` は認識できません"]
    if suggestions:
        parts.append(f"もしかして: {', '.join(suggestions)}")
    parts.append("有効なキー一覧は `pyfltr config list --all` で確認できます")
    return "。".join(parts)
