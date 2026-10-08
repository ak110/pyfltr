"""全体・プロジェクト設定の読込と統合。"""

import copy
import dataclasses
import pathlib
import typing

import tomlkit
import tomlkit.exceptions

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
    """デフォルト設定を生成。"""
    config = Config(
        values=copy.deepcopy(DEFAULT_CONFIG),
        commands=dict(pyfltr.tools.BUILTIN_COMMANDS),
        command_names=list(pyfltr.tools.BUILTIN_COMMAND_NAMES),
    )
    config.values["aliases"]["fast"] = build_fast_alias(config)
    return config


def read_config_text(path: pathlib.Path) -> str:
    """設定ファイルを読み込む。読み込めない場合は対象のパスと対処を含む`ValueError`を送出する。"""
    try:
        return path.read_text(encoding="utf-8")
    except OSError as e:
        raise ValueError(f"設定ファイルを読み込めません: {path}: {e}。ファイルの読み取り権限を確認してください") from e


def format_project_config_missing(path: pathlib.Path, *, use_global: str) -> str:
    """project側のpyproject.tomlが無い状態で設定を書き込もうとした場合の案内文を返す。

    CLIとMCPで同じ文面を使い、global設定を対象にする指定方法（`use_global`）だけを呼び出し側が渡す。
    """
    return (
        f"pyproject.tomlが見つかりません: {path}。"
        f"プロジェクトのルートで実行するか、global設定（XDG準拠）に書く場合は {use_global} を指定してください"
    )


def read_global_config(path: pathlib.Path) -> dict[str, typing.Any]:
    """globalのconfig.tomlを読み込み、`[tool.pyfltr]`配下を返す。

    ファイル不在時は空辞書を返す。TOML構文エラー時は`ValueError`で停止する。
    """
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
    """tomlkitの値を素のPython値（dict / list / 基本型）へ再帰的に変換する。

    tomlkitはInteger / String / Bool等のラッパー型で値を返す。
    `isinstance(v, int)`等の判定は通常通り動くが、`config.values`に格納すると
    JSON serializeや`==`比較で予期せぬ挙動になる場合があるため、
    入力段で純粋なPython値へ揃えておく。
    """
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
    """Global / project由来の`[tool.pyfltr]`辞書をマージし、由来情報を返す。

    通常キーはproject優先（globalの値はprojectで上書きされる）。
    `GLOBAL_PRIORITY_KEYS`に含まれるarchive/cache系はglobal優先で、
    両方にある場合はglobal値で上書きする。
    キー名の`_`は`-`に正規化してから判定する
    （同じキーが`archive_max_runs`と`archive-max-runs`で別計上されないように）。

    Returns:
        マージ済みdict（normalized key → value）と、
        normalized keyから`{"global", "project"}`部分集合への辞書のタプル。
    """
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
            # archive / cache系: globalがあればglobal、無ければproject。
            if "global" in sources:
                merged[key] = normalized_global[key]
            else:
                merged[key] = normalized_project[key]
        else:
            # 通常キー: 後勝ち（project優先）。
            if "project" in sources:
                merged[key] = normalized_project[key]
            else:
                merged[key] = normalized_global[key]
    return merged, key_sources


def make_config_warning_emitter(
    suppressed_warning_entries: frozenset[ConfigWarningEntry],
    warned_entries: set[ConfigWarningEntry],
) -> ConfigWarningEmitter:
    """抑止対象外の設定検証警告を発行するクロージャーを生成する。

    抑止判定は`(key, message)`の組の一致で行う。同一キーでも原因（警告本文）が
    異なる警告は抑止しない。
    発行した組は`warned_entries`（呼び出し側が用意した可変集合）へ追加する。
    """

    def emit(key: str, *, message: str) -> None:
        entry = (key, message)
        if entry in suppressed_warning_entries:
            return
        warned_entries.add(entry)
        pyfltr.warnings_.emit_warning(source="config", message=message)

    return emit


def load_config(
    config_dir: pathlib.Path | None = None,
    *,
    global_config_path: pathlib.Path | None = None,
    for_subproject: bool = False,
    suppressed_warning_entries: frozenset[ConfigWarningEntry] = frozenset(),
) -> Config:
    """pyproject.tomlとglobal設定ファイルから設定を読み込む。

    `config_dir`配下の`pyproject.toml`の`[tool.pyfltr]`と、
    XDG準拠のglobal設定ファイル`~/.config/pyfltr/config.toml`の`[tool.pyfltr]`を
    1つの入力dictへマージしてから、preset反映・custom-commands登録・
    言語カテゴリによる限定・通常設定適用の順で処理する。

    マージ仕様:
      - 通常キーはproject優先（後勝ち）
      - archive/cache系（`GLOBAL_PRIORITY_KEYS`）はglobal優先
    `pyproject.toml`不在でもglobal側のみ書かれていれば反映される
    （旧版にあった「pyproject.toml不在時の早期return」は撤廃済み）。

    検証の例外送出ポリシー:
      - 設定ファイル経由の検証（未知キー・型不一致・値不正・削除済みコマンド向けキー・
        preset値不正・カスタムコマンド定義の各項目・言語カテゴリキーの真偽値以外）は
        `pyfltr.warnings_.emit_warning(source="config")`で警告し、対象のキーは無視して
        既定値を維持する。カスタムコマンド定義が不正なら対象の定義の登録自体をスキップする。
        `custom-commands`配下がテーブル以外の場合はカスタムコマンド登録処理全体をスキップする。
        由来（global / project）による差は無く、両方に対し同じ警告経路を適用する。
        ただし`suppressed_warning_entries`と本ロード自身の由来判定の両方が
        「globalのみ由来」と示す警告は発行しない。
        複数バージョンのpyfltrが同じ設定ファイルを参照する状況での停止回避を主目的とする。
      - TOML構文エラーは続行不能のため`ValueError`で停止する。
      - CLI入力経路（`parse_config_value`相当）の値検証は単一バージョン内の対話入力を
        前提とするため、従来通り`ValueError`で停止する。

    Args:
        config_dir: project側の`pyproject.toml`を探すディレクトリ。
            未指定時はカレントディレクトリ。
        global_config_path: global設定ファイルのパス。未指定時は
            `default_global_config_path()`の結果を使用する。
        for_subproject: モノレポのサブプロジェクト別config解決として呼ばれた場合に`True`。
            リポジトリ単位ツールの実行可否は起点configだけが決めるため、
            サブプロジェクトの設定に基づく衝突警告（`warn_precommit_prek_conflict`）は
            誤検知になる。`True`のとき同警告を抑止する。
            設定ファイル不在の警告（`warn_config_files`）は本関数では発行せず、
            実行対象の確定後に実行パイプラインが起点cwdを基準に発行する。
        suppressed_warning_entries: 検証警告の抑止候補`(キー名, 警告本文)`集合。
            モノレポのサブプロジェクト解決では、`resolve_subproject_configs`がそのロード
            開始時点までに実際に発行済みのグローバル由来警告のスナップショットを渡す。
            実際に抑止するのは、この集合に含まれ、かつ本ロード自身の由来判定でも
            「globalのみ由来」となるキーの警告だけである
            （本ロード自身のproject側がそのキーを独自に上書きしている場合は
            抑止対象から外れ検証警告が発行される）。
            抑止の単位をキー名単独ではなく本文まで含めた組にするのは、同一キーに対して
            原因の異なる警告（値が不正 / キーを認識できない 等）が生じ得るためで、
            キー名一致だけで抑止すると別原因の警告まで失われる。
            一方で由来判定との併用は維持しており、独立した複数のproject設定が
            同じ誤設定を持つ場合の警告は抑止されない。
            起点cwdのロード（`for_subproject=False`）では通常渡さない
            （既定値`frozenset()`のため抑止対象は常に空になり、全キーを検証する）。
    """
    config = create_default_config()
    base = config_dir or pathlib.Path.cwd()

    # global側の読み込み（不在時は空dict）
    if global_config_path is None:
        global_config_path = default_global_config_path()
    global_data = read_global_config(global_config_path)

    # project側の読み込み（不在時は空dict）。
    # 旧実装にあった「pyproject.toml不在時の早期return」は撤廃済み。
    # globalだけが書かれている場合でも反映を成立させるため、
    # 不在時は空dictとして処理を継続する。
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

    # global / projectをマージ。各キーの由来も記録する。
    tool_pyfltr, key_sources = merge_global_and_project(global_data, project_data)
    effective_suppressed_entries = frozenset(
        entry for entry in suppressed_warning_entries if key_sources.get(entry[0]) == {"global"}
    )
    warned_entries: set[ConfigWarningEntry] = set()
    emit_config_warning = make_config_warning_emitter(effective_suppressed_entries, warned_entries)

    # archive/cache系がproject側に書かれていた場合の警告。
    # global側にも対象のキーがある場合のみ警告対象（global側に無ければproject値が
    # そのまま採用されるので警告不要）。
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
    """presetキーを読み取り、対応するプリセット設定をconfigに反映する。

    値が不正な場合（型不一致・未知名・削除済みpreset）は警告して未指定扱いに戻し、
    presetを反映せずに続行する。
    """
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
    """言語カテゴリgateを適用する（preset < 言語カテゴリgate < 個別設定）。

    v3.0.0でpython / javascript / rust / dotnetを同じ枠組みのカテゴリキーに統一した。
    presetは各時点の推奨構成として全言語のツールを横断的にTrueにするが、カテゴリ
    キーがFalse（既定）のときはpreset由来のTrueをFalseへ上書きして実行を抑止する。
    後続の個別設定ループで`{command} = true` / `{command} = false`による上書きが可能
    （個別指定はgateを越えて最優先）。

    言語カテゴリキーに真偽値以外を指定した場合は警告し、既定値`False`（gate閉鎖）として
    扱う。`bool(...)`暗黙変換による意図しないgate開放を回避するため、`isinstance`で
    厳密に判定する。
    """
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
            continue  # gate 開放: preset 由来の True をそのまま通す
        for cmd in commands:
            if cmd in user_keys:
                continue  # 個別設定による明示指定を保持 (True/False 双方)
            config.values[cmd] = False
