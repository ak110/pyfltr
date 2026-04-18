"""retry_command 生成および失敗ファイル絞り込み。"""

import logging
import os
import pathlib
import sys

import pyfltr.command
import pyfltr.paths

logger = logging.getLogger(__name__)

# 引数を別トークンで受け取るオプション (= なし形式) の集合。
# retry_command 整形時に位置引数と誤認しないよう明示する。
_VALUE_OPTIONS: frozenset[str] = frozenset(
    {
        "--commands",
        "--from-run",
        "--output-format",
        "--output-file",
        "--work-dir",
        "-j",
        "--jobs",
    }
)


def detect_launcher_prefix() -> list[str]:
    """retry_command の先頭に置くべき起動プレフィックスを推定する。

    Linux では ``/proc/self/status`` から親プロセスを辿り、先頭が ``uv`` で
    第 2 引数が ``run`` なら ``["uv", "run", "pyfltr"]``、先頭が ``uvx`` なら
    ``["uvx", "pyfltr"]`` を返す。macOS / Windows など親プロセスを取得できない
    環境では ``[sys.argv[0]]`` の basename にフォールバックする。
    """
    fallback = [os.path.basename(sys.argv[0]) or "pyfltr"]
    try:
        with open("/proc/self/status", encoding="utf-8") as f:
            ppid_line = next((line for line in f if line.startswith("PPid:")), None)
        if ppid_line is None:
            return fallback
        ppid = ppid_line.split(":", 1)[1].strip()
        with open(f"/proc/{ppid}/cmdline", "rb") as f:
            raw = f.read()
    except OSError:
        return fallback
    tokens = [tok.decode("utf-8", errors="replace") for tok in raw.split(b"\0") if tok]
    if not tokens:
        return fallback
    launcher = os.path.basename(tokens[0])
    if launcher == "uv" and len(tokens) >= 2 and tokens[1] == "run":
        return ["uv", "run", "pyfltr"]
    if launcher == "uvx":
        return ["uvx", "pyfltr"]
    return fallback


def build_retry_args_template(sys_args: list[str]) -> list[str]:
    """起動時 argv (サブコマンド以降) を retry_command 用テンプレートとして整形する。

    ``--commands`` の値は後段の per-tool 差し替え用に空文字プレースホルダへ置換し、
    位置引数 (ターゲット) は末尾から除去する (後段で当該ツールのファイル一覧で
    置換される前提)。``--no-fix`` や ``--output-format`` 等は保持する。
    ``--only-failed`` フラグおよび ``--from-run`` オプション (値あり・= 付き両形式) は
    再実行時に直前 run を暗黙参照しないよう除去する。
    """
    result: list[str] = []
    i = 0
    # 実行系サブコマンド (ci / run / fast / run-for-agent) は先頭に必ず来る想定。
    # これを保持することで pyfltr ci 失敗時に fix ステージが暴発しない。
    while i < len(sys_args):
        arg = sys_args[i]
        if arg == "--commands":
            # 後段で当該ツール 1 件に差し替えるためプレースホルダを置く。
            result.extend([arg, ""])
            i += 2
            continue
        if arg.startswith("--commands="):
            result.append("--commands=")
            i += 1
            continue
        # --only-failed は再実行時に直前 run を暗黙参照するため除去する。
        if arg == "--only-failed":
            i += 1
            continue
        # --from-run <value> (空白区切り) を除去する。
        if arg == "--from-run":
            i += 2  # フラグと値を両方スキップ
            continue
        # --from-run=<value> (等号区切り) を除去する。
        if arg.startswith("--from-run="):
            i += 1
            continue
        result.append(arg)
        i += 1
    return result


def build_retry_command(
    args_template: list[str],
    launcher_prefix: list[str],
    *,
    tool: str,
    target_files: list[pathlib.Path],
    original_cwd: str,
) -> str:
    """Tool レコードへ埋め込む retry_command 文字列を生成する。

    ``args_template`` の ``--commands`` プレースホルダを当該ツールに差し替え、
    位置引数 (ターゲット) を ``target_files`` で末尾に再配置する。ターゲットは
    ``original_cwd`` 基準の絶対パスに変換することで、``--work-dir`` と cwd の
    二重解釈を避ける。
    """
    import shlex  # pylint: disable=import-outside-toplevel

    cwd_path = pathlib.Path(original_cwd)
    # 位置引数 (サブコマンドを除く、`-` で始まらないトークンの末尾側) は除去する。
    # サブコマンドは必ず最初の非オプショントークンのため、それだけ残す。
    filtered: list[str] = []
    seen_subcommand = False
    i = 0
    while i < len(args_template):
        arg = args_template[i]
        # オプション引数 (=付き・=なし両対応) はそのまま保持する。
        # ``--commands`` プレースホルダ経由のみ後段で差し替え対象とする。
        if arg.startswith("-"):
            filtered.append(arg)
            # 引数を伴うオプションか確認 (値が付いていない場合は次のトークンも引き取る)。
            if "=" not in arg and i + 1 < len(args_template):
                next_arg = args_template[i + 1]
                if not next_arg.startswith("-") and _option_takes_value(arg):
                    filtered.append(next_arg)
                    i += 2
                    continue
            i += 1
            continue
        # 最初の非オプショントークンはサブコマンド扱いで保持する。
        if not seen_subcommand:
            filtered.append(arg)
            seen_subcommand = True
            i += 1
            continue
        # それ以降の位置引数 (= ターゲット) は捨てる。後段で target_files で差し替える。
        i += 1

    # --commands プレースホルダを当該ツールで埋める。
    replaced: list[str] = []
    j = 0
    commands_replaced = False
    while j < len(filtered):
        arg = filtered[j]
        if arg == "--commands":
            replaced.extend(["--commands", tool])
            commands_replaced = True
            j += 2 if j + 1 < len(filtered) else 1
            continue
        if arg == "--commands=":
            replaced.append(f"--commands={tool}")
            commands_replaced = True
            j += 1
            continue
        if arg.startswith("--commands="):
            replaced.append(f"--commands={tool}")
            commands_replaced = True
            j += 1
            continue
        replaced.append(arg)
        j += 1
    # --commands 未指定だった場合は追記する (サブコマンドの直後に挿入)。
    if not commands_replaced:
        insert_at = 0
        for k, tok in enumerate(replaced):
            if not tok.startswith("-"):
                insert_at = k + 1
                break
        replaced[insert_at:insert_at] = ["--commands", tool]

    # ターゲットを元 cwd 基準の絶対パスに変換して末尾に追加する。
    target_strs: list[str] = []
    for target in target_files:
        if target.is_absolute():
            target_strs.append(str(target))
        else:
            target_strs.append(str((cwd_path / target).resolve(strict=False)))

    parts: list[str] = [*launcher_prefix, *replaced, *target_strs]
    return shlex.join(parts)


def populate_retry_command(
    result: pyfltr.command.CommandResult,
    *,
    retry_args_template: list[str],
    launcher_prefix: list[str],
    original_cwd: str,
) -> None:
    """CommandResult に retry_command を埋める (パートG A案の絞り込みを適用)。

    キャッシュ復元結果 (``result.cached == True``) では retry_command を埋めない
    (再実行不要のため)。それ以外では ``filter_failed_files`` で失敗ファイルのみに
    絞り込んだターゲットを ``build_retry_command`` へ渡す。絞り込み結果が空の場合
    (診断ファイルなし・全体失敗のみのケース) は retry_command のターゲット位置
    引数が空になる (当該ツールの単体再実行文字列として機能する)。
    """
    if result.cached:
        return
    filtered_targets = filter_failed_files(result)
    result.retry_command = build_retry_command(
        retry_args_template,
        launcher_prefix,
        tool=result.command,
        target_files=filtered_targets,
        original_cwd=original_cwd,
    )


def filter_failed_files(result: pyfltr.command.CommandResult) -> list[pathlib.Path]:
    """``result.errors`` から失敗ファイル集合を抽出し ``result.target_files`` と交差させる。

    ``retry_command`` のターゲットを「当該ツールで失敗したファイルのみ」に絞る用途
    (パートG A案)。パス比較は文字列化した相対パス (スラッシュ区切り) で行う。
    ``ErrorLocation.file`` は ``_normalize_path`` 経由で cwd 基準の相対パス (区切り
    文字は ``/``) に正規化されているため、``result.target_files`` 側も同じ表現へ
    揃えたうえで比較する。並び順は ``result.target_files`` の順序を保つ。

    ``result.errors`` が空、または ``ErrorLocation.file`` 集合と ``result.target_files``
    の交差が空の場合は空リストを返す (pytest 等の pass-filenames=False で全体失敗
    のみのケース。呼び出し側で retry_command のターゲット位置引数を空にする前提)。
    """
    if not result.errors:
        return []
    failed_files = {error.file for error in result.errors if error.file}
    if not failed_files:
        return []
    filtered: list[pathlib.Path] = []
    for target in result.target_files:
        normalized = pyfltr.paths.normalize_separators(target)
        if normalized in failed_files:
            filtered.append(target)
    return filtered


def _option_takes_value(opt: str) -> bool:
    """オプションが次のトークンを値として取るかを判定する。

    ``--foo=bar`` 形式は判定対象外 (呼び出し側で `=` の有無を先に確認する前提)。
    ビルトイン / カスタムコマンドの ``--{cmd}-args`` も値を伴うため、末尾が
    ``-args`` で終わるものは全て True として扱う。
    """
    if opt.endswith("-args"):
        return True
    return opt in _VALUE_OPTIONS
