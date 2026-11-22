import argparse
import json
import re
import sys
import tokenize
from collections import Counter
from pathlib import Path

from pyfltr.comment_check.check import SUFFIXES, candidates

_MESSAGE = (
    "コメントではなくコード・型・lintで表現してください。"
    "例外は必須ディレクティブ、外部制約、変更連動の案内、実行時に参照される文書、法的表記です。"
    "不要になった許可は --update-baseline で削除できます。"
)


def _load(path: Path) -> dict[str, Counter[str]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(data, dict):
        raise ValueError("ベースラインはファイルパスをキーとするJSONオブジェクトにしてください。")
    result = {}
    for file, counts in data.items():
        if not isinstance(counts, dict) or any(
            not isinstance(count, int) or isinstance(count, bool) or count < 1 for count in counts.values()
        ):
            raise ValueError(f"{file} の許可件数は正の整数にしてください。")
        result[file] = Counter(counts)
    return result


def _check(args: argparse.Namespace) -> int:
    root = Path.cwd()
    path = args.baseline
    baseline = {} if args.init_baseline else _load(path)
    allow = [re.compile(pattern) for pattern in args.allow_pattern]
    failed = False
    seen: set[str] = set()
    for file in args.files:
        suffix = file.suffix.lower()
        if suffix not in SUFFIXES:
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
                raise ValueError(f"ベースラインの対象をカレントディレクトリ配下に指定してください: {file}")
            current = Counter(comment.normalized for comment in comments)
            baseline[key] = current if args.init_baseline else current & allowed
        else:
            for comment in comments:
                if allowed[comment.normalized]:
                    allowed[comment.normalized] -= 1
                else:
                    print(f"{file}:{comment.line}:{comment.column}: error: {_MESSAGE}")
                    failed = True
    if not failed and (args.init_baseline or args.update_baseline):
        payload = {key: counts for key, counts in baseline.items() if counts and (root / key).is_file()}
        path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return int(failed)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ソースコードのコメントを明示的な例外とベースラインに照らして検査します。")
    parser.add_argument("--baseline", type=Path, default=Path(".pyfltr-comment-baseline.json"))
    parser.add_argument("--allow-pattern", action="append", default=[])
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument("--init-baseline", action="store_true")
    operation.add_argument("--update-baseline", action="store_true")
    parser.add_argument("files", nargs="+", type=Path)
    try:
        return _check(parser.parse_args(argv))
    except (OSError, ValueError, re.error) as exc:
        print(f"コメントの検査を続行できません: {exc}", file=sys.stderr)
        return 2
