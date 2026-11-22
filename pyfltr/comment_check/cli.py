import argparse
import json
import re
import sys
import tokenize
from collections import Counter
from pathlib import Path

from pyfltr.comment_check.check import SUFFIXES, candidates

_MESSAGE = (
    "許可されていないコメントです。不要なら削除し、意図はコード・型・lintで表してください。"
    "残す必要がある場合は、本文へ`pyfltr: allow-comment`を加えるか、"
    "`--allow-pattern`（pyfltr経由では`comment-check-extend-args`）の正規表現で許可してください。"
    "導入前からあるコメントは"
    '`pyfltr run "{file}" --commands=comment-check --enable=comment-check --comment-check-args=--init-baseline`で'
    "ベースラインへ登録できます。"
)


def _load(path: Path) -> dict[str, Counter[str]]:
    remedy = f"{path}を直すか、削除してから--init-baselineで作り直してください。"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        raise ValueError(f"ベースライン{path}をJSONとして読めません（{exc}）。{remedy}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"ベースライン{path}がファイルパスをキーとするJSONオブジェクトではありません。{remedy}")
    result = {}
    for file, counts in data.items():
        if not isinstance(counts, dict) or any(
            not isinstance(count, int) or isinstance(count, bool) or count < 1 for count in counts.values()
        ):
            raise ValueError(f"ベースライン{path}の{file}の許可件数が正の整数ではありません。{remedy}")
        result[file] = Counter(counts)
    return result


def _compile(pattern: str) -> re.Pattern[str]:
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise ValueError(
            f"--allow-patternの正規表現{pattern!r}が不正です（{exc}）。"
            "Pythonの正規表現として直してください（pyfltr経由ではcomment-check-extend-argsの値）。"
        ) from exc


def _check(args: argparse.Namespace) -> int:
    root = Path.cwd()
    path = args.baseline
    for file in args.files:
        if file.is_dir() and not (file.is_symlink() or (file / ".git").exists() or not any(file.iterdir())):
            raise ValueError(
                f"{file}はディレクトリです。ファイルを指定するか、"
                "pyfltr run --commands=comment-check --enable=comment-checkで対象のファイルを一括で渡してください。"
            )
    baseline = _load(path)
    allow = [_compile(pattern) for pattern in args.allow_pattern or []]
    failed = False
    seen: set[str] = set()
    for file in args.files:
        suffix = file.suffix.lower()
        if suffix not in SUFFIXES or file.is_dir():
            continue
        try:
            key = file.resolve().relative_to(root).as_posix()
        except ValueError:
            key = file.resolve().as_posix()
        if key in seen:
            continue
        seen.add(key)
        try:
            if suffix in {".py", ".pyi"}:
                with tokenize.open(file) as stream:
                    source = stream.read()
            else:
                source = file.read_text(encoding="utf-8-sig")
            comments = list(candidates(source, suffix, allow))
        except (OSError, UnicodeError, SyntaxError, tokenize.TokenError) as exc:
            print(f"{file}:1:1: error: コメントを検査できません: {exc}。構文・文字コード・読取権限を確認してください。")
            failed = True
            continue
        allowed = baseline.get(key, Counter())
        if args.init_baseline or args.update_baseline:
            if Path(key).is_absolute():
                raise ValueError(
                    f"{file}はカレントディレクトリの外にあるため、ベースラインへ登録できません。"
                    "ベースラインの基準にするディレクトリへ移動し、その配下のファイルを指定してください。"
                )
            current = Counter(comment.normalized for comment in comments)
            baseline[key] = current if args.init_baseline else current & allowed
        else:
            for comment in comments:
                if allowed[comment.normalized]:
                    allowed[comment.normalized] -= 1
                else:
                    print(f"{file}:{comment.line}:{comment.column}: error: {_MESSAGE.format(file=file)}")
                    failed = True
    if not failed and (args.init_baseline or args.update_baseline):
        payload = {key: counts for key, counts in baseline.items() if counts and (root / key).is_file()}
        path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return int(failed)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ソースコードのコメントを明示的な例外とベースラインに照らして検査します。")
    parser.add_argument(
        "--baseline",
        type=Path,
        default=Path(".pyfltr-comment-baseline.json"),
        help="ベースラインのパス（既定: カレントディレクトリの.pyfltr-comment-baseline.json）",
    )
    parser.add_argument("--allow-pattern", action="append", help="許可するコメント本文の正規表現（複数指定可）")
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument("--init-baseline", action="store_true", help="指定したファイルの許可を現在のコメントで置き換える")
    operation.add_argument("--update-baseline", action="store_true", help="指定したファイルの許可から現在無いコメントを除く")
    parser.add_argument(
        "files",
        nargs="+",
        type=Path,
        help="検査するファイル（ディレクトリは指定できない。ディレクトリへのシンボリックリンク、.gitを持つディレクトリ、空のディレクトリは対象外とする）",
    )
    try:
        return _check(parser.parse_args(argv))
    except ValueError as exc:
        print(f"コメントの検査を続行できません: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"コメントの検査を続行できません: {exc}。パスと読み書きの権限を確認してください。", file=sys.stderr)
        return 2
