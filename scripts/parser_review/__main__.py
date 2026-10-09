"""エラーパーサーの網羅レビューと互換性確認で使う採取・比較の開発用コマンド。

リポジトリルートから`uv run python -m scripts.parser_review <サブコマンド>`で起動する。

- `collect`: 対応ツールのサンプルをpyfltr経由で実行し、生出力・診断・コマンドラインと採取状態を記録する
- `pytest-forms`: pytest出力の解析レビューで確認する形態と派生を採取・生成する
- `compare`: 採取した生出力を基準commitと作業ツリーの解析処理で解析し、診断全件を比較する

採取の終了コードは、中断・全体の時間上限・停止失敗のいずれかが起きた場合に1となる。
ツール未導入などで未採取になったツールは集計の`not_collected`へ理由とともに記録し、終了コードには反映しない。
"""

import argparse
import json
import pathlib
import signal
import sys

from scripts.parser_review import collect, compare, pytest_forms, samples


def _add_collect_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--output", required=True, type=pathlib.Path, help="採取成果物の出力ディレクトリ")
    parser.add_argument("--timeout", type=float, default=300.0, help="採取1件（準備コマンドとpyfltr実行の各々）の時間上限秒数")
    parser.add_argument("--total-timeout", type=float, default=3600.0, help="採取全体の時間上限秒数")


def _split(value: str | None) -> list[str] | None:
    if value is None:
        return None
    return [item.strip() for item in value.split(",") if item.strip()]


def _collector(args: argparse.Namespace) -> collect.Collector:
    collector = collect.Collector(output_dir=args.output.resolve(), timeout=args.timeout, total_timeout=args.total_timeout)

    def _request_stop(signum: int, frame: object) -> None:
        del signum, frame
        collector.stop_event.set()

    signal.signal(signal.SIGINT, _request_stop)
    return collector


def _finish(collector: collect.Collector, records: list[dict]) -> int:
    summary = collect.write_results(collector.output_dir, records, incomplete=collector.incomplete)
    print(json.dumps({key: summary[key] for key in ("incomplete", "counts", "total")}, ensure_ascii=False))
    print(f"summary: {collector.output_dir / collect.SUMMARY_FILENAME}")
    print(f"inputs: {collector.output_dir / collect.INPUTS_FILENAME}")
    return 1 if collector.incomplete else 0


def main(argv: list[str] | None = None) -> int:
    """サブコマンドを実行する。"""
    parser = argparse.ArgumentParser(
        prog="python -m scripts.parser_review",
        description="エラーパーサーの網羅レビューと互換性確認で使う採取・比較の開発用コマンド",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    collect_parser = subparsers.add_parser("collect", help="対応ツールのサンプルを採取する")
    selection = collect_parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--all", action="store_true", help="BUILTIN_COMMANDSの全ツールを採取する")
    selection.add_argument("--tools", help="採取するツール名（カンマ区切り）")
    _add_collect_options(collect_parser)

    forms_parser = subparsers.add_parser("pytest-forms", help="pytest出力の確認形態を採取・生成する")
    forms_parser.add_argument("--forms", help="形態番号または形態ID（カンマ区切り）。省略時は全形態")
    forms_parser.add_argument("--list", action="store_true", help="形態の一覧を表示して終了する")
    forms_parser.add_argument("--output", type=pathlib.Path, help="採取成果物の出力ディレクトリ")
    forms_parser.add_argument("--timeout", type=float, default=300.0, help="採取1件の時間上限秒数")
    forms_parser.add_argument("--total-timeout", type=float, default=3600.0, help="採取全体の時間上限秒数")

    compare_parser = subparsers.add_parser("compare", help="基準commitと作業ツリーの解析結果を比較する")
    compare_parser.add_argument("--input", required=True, type=pathlib.Path, help="collectまたはpytest-formsの出力ディレクトリ")
    compare_parser.add_argument("--output", required=True, type=pathlib.Path, help="比較結果JSONの出力先")
    compare_parser.add_argument("--base", default="HEAD", help="基準commit（既定: HEAD）")

    args = parser.parse_args(argv)
    try:
        if args.command == "collect":
            selected = samples.select_samples(None if args.all else _split(args.tools))
            collector = _collector(args)
            return _finish(collector, collect.collect(collector, selected))
        if args.command == "pytest-forms":
            if args.list:
                print(pytest_forms.dump_catalog())
                return 0
            if args.output is None:
                parser.error("pytest-formsには--outputが必要です（--listを除く）")
            forms = pytest_forms.select_forms(_split(args.forms))
            collector = _collector(args)
            return _finish(collector, pytest_forms.generate(collector, forms))
        report = compare.compare(args.input.resolve(), args.output.resolve(), base=args.base)
        print(
            json.dumps(
                {"counts": report["counts"], "total": report["total"], "base": report["base"]["commit"]}, ensure_ascii=False
            )
        )
        print(f"report: {args.output.resolve()}")
        return 0
    except (ValueError, compare.CompareError) as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
