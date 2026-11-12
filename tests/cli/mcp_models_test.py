"""MCP応答モデルの公開項目と用途の対照試験。

MCPの応答はエージェントが作業の判断・操作に使う情報だけを持つ。
内部に保存している値や人間向けの表示値を、取得できるという理由だけで応答へ加えない。
本試験は全ツールの戻り値から到達する応答モデルの項目を列挙し、用途を記録した対照表と比べる。
項目の追加・削除で失敗させ、追加する項目には受信するエージェントが行う判断・操作を表へ書かせる。
"""

import inspect
import pathlib
import typing

import pydantic
import pytest

import pyfltr.cli.mcp_grep_tools
import pyfltr.cli.mcp_runs_tools
import pyfltr.cli.mcp_server
import pyfltr.config.config
import pyfltr.grep_.jsonl_records

# 項目ごとに、エージェントがその値で行う判断・操作を記録する。
_FIELD_PURPOSES: dict[str, str] = {
    # run
    "RunResult.run_id": "詳細取得（show_run系）の参照キー",
    "RunResult.exit_code": "成否の判定",
    "RunResult.completion": "対象到達と完了の判定（最初に読む）",
    "RunResult.files_reached": "対象へ到達したかの確認",
    "RunResult.completed_commands": "完了したチェックの確認",
    "RunResult.incomplete_commands": "未完了のチェックと再実行対象の特定",
    "RunResult.missing_targets": "指定パスの誤りの修正",
    "RunResult.fully_excluded_files": "除外設定で対象外になった指定の確認",
    "RunResult.failed": "診断を取得するコマンドの特定",
    "RunResult.commands": "コマンド別の状態と診断件数の確認",
    "RunResult.skipped_reason": "実行されなかった理由の確認",
    "RunResult.retry_commands": "失敗コマンドの再実行",
    "RunResult.warnings": "警告の対処",
    "RunResult.guidance": "次の操作の選択",
    "RunWarningModel.source": "警告の発生源の特定",
    "RunWarningModel.message": "警告の原因と結果の把握",
    "RunWarningModel.hint": "警告への対処",
    "CommandSummaryModel.command": "コマンドの特定",
    "CommandSummaryModel.status": "コマンド別の成否",
    "CommandSummaryModel.diagnostics": "診断の有無と件数",
    "CommandSummaryModel.elapsed": "遅いツールの特定と検証時間の分析",
    "CommandSummaryModel.started_at": "並行区間と待機を含む所要の分析",
    "CommandSummaryModel.finished_at": "並行区間と待機を含む所要の分析",
    "CommandSummaryModel.slow_tests": "遅いテストの改善対象の特定",
    "SlowTestModel.nodeid": "遅いテストの特定",
    "SlowTestModel.phase": "遅い区間（setup・call・teardown）の特定",
    "SlowTestModel.seconds": "遅さの程度の比較",
    # list_runs・show_run
    "RunSummaryModel.run_id": "対象runの選択",
    "RunSummaryModel.started_at": "対象runの選択",
    "RunSummaryModel.finished_at": "完了・実行中の判別",
    "RunSummaryModel.exit_code": "成否の判定",
    "RunSummaryModel.commands": "対象runの選択",
    "RunSummaryModel.files": "対象runの選択",
    "RunSummaryModel.cwd": "別の作業ディレクトリのrunの選択",
    "RunOverviewModel.run_id": "参照中のrunの確認",
    "RunOverviewModel.meta": "runの対象・起動引数・結果・時刻の確認（項目はSHOW_RUN_META_KEYSで限定）",
    "RunOverviewModel.timing": "待機と所要の分析",
    "RunOverviewModel.commands": "コマンド別の状態と診断件数の確認",
    "RunTimingModel.measured": "待機の未計測と0秒の区別",
    "RunTimingModel.run_elapsed": "run全体の所要の分析",
    "RunTimingModel.slot_waits": "他の検証を待った区間の分析",
    "RunTimingModel.slot_wait_total": "待機合計の分析",
    "RunTimingModel.unfinished_slot_waits": "未完了の待機の判別",
    "SlotWaitModel.kind": "初回待ちと取り直し待ちの区別",
    "SlotWaitModel.sequence": "区間の対応付け",
    "SlotWaitModel.started_at": "待機区間の時刻の分析",
    "SlotWaitModel.finished_at": "待機区間の時刻の分析",
    "SlotWaitModel.elapsed": "待機秒数の分析",
    "SlotWaitModel.outcome": "取得・中断などの終了理由の判別",
    # show_run_diagnostics
    "CommandDiagnosticsModel.command_meta": "コマンドの状態・終了コード・再実行手段の確認",
    "CommandDiagnosticsModel.diagnostics": "修正する指摘の取得",
    "CommandDiagnosticsModel.hint_urls": "ルールの説明への到達",
    "CommandDiagnosticsModel.hints": "修正方法の手掛かり",
    "CommandDiagnosticsModel.total_diagnostics": "分割取得の総数",
    "CommandDiagnosticsModel.next_offset": "分割取得の続き",
    "CommandMetaModel.command": "コマンドの特定",
    "CommandMetaModel.type": "formatter・linter・testerの区別による対処の選択",
    "CommandMetaModel.status": "コマンドの成否",
    "CommandMetaModel.returncode": "ツール自体の失敗と診断の区別",
    "CommandMetaModel.files": "対象数の確認",
    "CommandMetaModel.elapsed": "所要の分析",
    "CommandMetaModel.started_at": "所要の分析",
    "CommandMetaModel.finished_at": "所要の分析",
    "CommandMetaModel.diagnostics": "診断件数",
    "CommandMetaModel.slow_tests": "遅いテストの特定",
    "CommandMetaModel.retry_command": "失敗ファイルに限定した再実行",
    "DiagnosticModel.command": "指摘の発生元",
    "DiagnosticModel.file": "修正するファイル",
    "DiagnosticModel.messages": "個別の指摘",
    "DiagnosticMessageModel.line": "修正位置",
    "DiagnosticMessageModel.col": "修正位置",
    "DiagnosticMessageModel.end_line": "修正範囲",
    "DiagnosticMessageModel.end_col": "修正範囲",
    "DiagnosticMessageModel.rule": "ルールの特定と抑止の判断",
    "DiagnosticMessageModel.severity": "対処の優先度",
    "DiagnosticMessageModel.fix": "自動修正の可否",
    "DiagnosticMessageModel.msg": "指摘内容",
    # show_run_output
    "OutputRangeModel.text": "生出力の確認",
    "OutputRangeModel.offset": "分割取得の位置",
    "OutputRangeModel.returned_lines": "分割取得の位置",
    "OutputRangeModel.total_lines": "分割取得の総量",
    "OutputRangeModel.next_offset": "分割取得の続き",
    # grep
    "GrepResultModel.matches": "一致箇所の確認",
    "GrepResultModel.file_results": "縮約時のファイル別結果",
    "GrepResultModel.output_mode": "縮約の有無の判別",
    "GrepResultModel.total_matches": "一致件数",
    "GrepResultModel.files_scanned": "走査範囲の確認",
    "GrepResultModel.exit_code": "一致の有無",
    "GrepResultModel.returned_matches": "省略の程度",
    "GrepResultModel.omitted_matches": "省略の有無と再検索の判断",
    "GrepResultModel.omitted_files": "省略の有無と再検索の判断",
    "GrepResultModel.guidance": "再検索・置換への手順",
    "GrepResultModel.warnings": "警告の対処",
    "GrepResultModel.fully_excluded_files": "除外された指定の確認",
    "GrepResultModel.missing_targets": "指定パスの誤りの修正",
    "GrepResultModel.summary_mode": "集計モードの確認",
    "GrepResultModel.files_with_matches": "一致ファイルの一覧",
    "GrepResultModel.file_counts": "ファイル別件数",
    "GrepResultModel.files_without_match": "不一致ファイルの一覧",
    "GrepMatchModel.file": "一致箇所のファイル",
    "GrepMatchModel.line": "一致箇所の行",
    "GrepMatchModel.col": "一致箇所の列",
    "GrepMatchModel.end_col": "一致範囲",
    "GrepMatchModel.match_text": "一致文字列",
    "GrepMatchModel.line_text": "一致行の文脈",
    "GrepMatchModel.before": "前後の文脈",
    "GrepMatchModel.after": "前後の文脈",
    "GrepMatchModel.line_text_offset": "切り出した行の列位置の解釈",
    "GrepMatchModel.truncated": "切り詰めの有無",
    "GrepFileCountModel.file": "ファイルの特定",
    "GrepFileCountModel.count": "ファイル別件数",
    "GrepFileResultModel.file": "ファイルの特定",
    "GrepFileResultModel.count": "省略前の件数",
    "GrepFileResultModel.matches": "返却した一致",
    # replace
    "ReplaceResultModel.replace_id": "取り消し（replace_undo）の参照キー",
    "ReplaceResultModel.dry_run": "書き換えの有無の確認",
    "ReplaceResultModel.files_changed": "影響ファイル数",
    "ReplaceResultModel.total_replacements": "置換件数",
    "ReplaceResultModel.file_changes": "ファイル別の置換件数",
    "ReplaceResultModel.changes": "置換前後の確認",
    "ReplaceResultModel.exit_code": "成否",
    "ReplaceResultModel.fully_excluded_files": "除外された指定の確認",
    "ReplaceResultModel.missing_targets": "指定パスの誤りの修正",
    "ReplaceResultModel.warnings": "警告の対処",
    "ReplaceFileChangeModel.file": "変更ファイル",
    "ReplaceFileChangeModel.count": "ファイル別の置換件数",
    "ReplaceChangeRecordModel.file": "置換箇所のファイル",
    "ReplaceChangeRecordModel.line": "置換箇所の行",
    "ReplaceChangeRecordModel.col": "置換箇所の列",
    "ReplaceChangeRecordModel.before_line": "置換前の確認",
    "ReplaceChangeRecordModel.after_line": "置換後の確認",
    "ReplaceUndoModel.replace_id": "取り消した履歴の確認",
    "ReplaceUndoModel.restored": "復元したファイル",
    "ReplaceUndoModel.skipped": "復元しなかったファイルと強制の判断",
    "ReplaceUndoModel.exit_code": "成否",
    "ReplaceUndoModel.warnings": "警告の対処",
    "ReplaceHistoryModel.action": "応答の種類",
    "ReplaceHistoryModel.entries": "履歴の選択",
    "ReplaceHistoryEntryModel.replace_id": "取り消す履歴の選択",
    "ReplaceHistoryEntryModel.saved_at": "取り消す履歴の選択",
    "ReplaceHistoryEntryModel.command": "履歴の置換内容の確認",
    "ReplaceHistoryEntryModel.files": "履歴の対象ファイル",
    "ReplaceHistoryFileModel.file": "履歴の対象ファイル",
    "ReplaceHistoryFileModel.records_count": "ファイル別の置換件数",
    # command_info・config
    "CommandInfoModel.command": "対象ツールの確認",
    "CommandInfoModel.resolved": "起動方式の解決の成否",
    "CommandInfoModel.info": "起動方式の解決失敗の切り分け（command_infoの目的そのもの）",
    "ConfigResultModel.action": "応答の種類",
    "ConfigResultModel.path": "操作した設定ファイル",
    "ConfigResultModel.key": "対象キー",
    "ConfigResultModel.value": "設定値",
    "ConfigResultModel.is_default": "既定値由来かの判別",
    "ConfigResultModel.existed": "削除対象の有無",
    "ConfigResultModel.values": "設定値の一覧",
    "ConfigResultModel.warnings": "警告の対処",
}


# 中身を応答モデルで列挙しない辞書型の項目と、キーを限定・検査する手段。
# 辞書の素通しは新しいキーを対照表へ現さないため、項目ごとにキーの出所と限定方法を記録する。
_DICT_FIELD_POLICIES: dict[str, str] = {
    "RunOverviewModel.meta": "mcp_runs_tools.SHOW_RUN_META_KEYS で返すキーを限定する",
    "CommandInfoModel.info": "_COMMAND_INFO_KEY_PURPOSES と照合する（test_command_info_keys_have_recorded_purposes）",
    "ReplaceHistoryEntryModel.command": "mcp_grep_tools.REPLACE_HISTORY_COMMAND_KEYS で返すキーを限定する",
    "GrepFileResultModel.matches": (
        "mcp_grep_tools.GREP_FILE_MATCH_KEYS（GrepMatchModelの項目からfile除く）で返すキーを限定する"
    ),
    "CommandDiagnosticsModel.hint_urls": "キーはルールID、値はURL。ツールの診断から生成し追加のキーを持たない",
    "CommandDiagnosticsModel.hints": "キーはルールID、値は修正ヒント。ツールの診断から生成し追加のキーを持たない",
    "RunResult.retry_commands": "キーはコマンド名、値は再実行のシェルコマンド",
    "ConfigResultModel.value": "利用者が指定した設定キーの値",
    "ConfigResultModel.values": "キーは設定キー、値は利用者の設定値",
}

# `command_info`の`info`のキーごとの用途。起動方式の解決失敗を切り分けることがこのツールの目的である。
_COMMAND_INFO_KEY_PURPOSES: dict[str, str] = {
    "enabled": "設定で有効かの確認",
    "runner": "設定上の起動方式",
    "runner_source": "起動方式を決めた設定の出所",
    "configured_path": "設定した実行ファイルのパス",
    "configured_args": "設定した引数",
    "configured_extend_args": "設定した追加引数",
    "severity": "失敗の扱いの確認",
    "hints": "設定した修正ヒント",
    "version": "設定した版指定",
    "dotnet_root": ".NETツールの解決環境",
    "mise_active_tool_key": "miseの有効ツールとの照合キー",
    "mise_active_tools": "miseの有効ツールの取得結果",
    "mise_tool_spec_omitted": "miseのtool spec省略の採用",
    "mise_check_hint": "miseの確認の手掛かり",
    "error": "解決失敗の理由",
    "effective_runner": "実際に採用した起動方式",
    "executable": "解決した実行ファイル",
    "executable_resolved": "実行ファイルが見つかったか",
    "commandline": "最終コマンドライン",
    "fix_commandline": "fixステージの最終コマンドライン",
    "uv_info": "uv経路の可用性とフォールバックの診断",
    "check_passed": "事前確認の成否",
    "check_error": "事前確認の失敗理由",
    "check_commandline": "事前確認で解決したコマンドライン",
    "check_effective_runner": "事前確認で採用した起動方式",
    "check_installed_version": "事前確認で取得した実行版",
}


def _collect_fields() -> set[str]:
    """全ツールの戻り値から到達する応答モデルの`<モデル名>.<項目名>`を返す。"""
    fields: set[str] = set()
    seen: set[type] = set()

    def _walk(annotation: typing.Any) -> None:
        for argument in typing.get_args(annotation):
            _walk(argument)
        if isinstance(annotation, type) and issubclass(annotation, pydantic.BaseModel) and annotation not in seen:
            seen.add(annotation)
            for name, field in annotation.model_fields.items():
                fields.add(f"{annotation.__name__}.{name}")
                _walk(field.annotation)

    for handler in pyfltr.cli.mcp_server.TOOL_HANDLERS.values():
        _walk(inspect.signature(handler, eval_str=True).return_annotation)
    return fields


def test_response_fields_have_recorded_purposes() -> None:
    """応答モデルの全項目が、エージェントの判断・操作を記録した対照表と一致する。"""
    actual = _collect_fields()
    added = sorted(actual - _FIELD_PURPOSES.keys())
    removed = sorted(_FIELD_PURPOSES.keys() - actual)
    assert not added and not removed, (
        "MCP応答の項目と用途の対照表が一致しません。"
        f"追加された項目: {added}、対照表にだけある項目: {removed}。"
        "追加する項目は、受信するエージェントがその値で行う判断・操作を_FIELD_PURPOSESへ書いてください。"
        "内部の保存値・人間向けの表示値・他の項目との重複は応答へ加えないでください。"
    )
    assert all(purpose.strip() for purpose in _FIELD_PURPOSES.values())


def _collect_dict_fields() -> set[str]:
    """応答モデルの項目のうち、注釈に辞書または任意の型を含むものを返す。"""
    fields: set[str] = set()
    seen: set[type] = set()

    def _contains_dict(annotation: typing.Any) -> bool:
        if annotation is typing.Any or typing.get_origin(annotation) is dict or annotation is dict:
            return True
        return any(_contains_dict(argument) for argument in typing.get_args(annotation))

    def _walk(annotation: typing.Any) -> None:
        for argument in typing.get_args(annotation):
            _walk(argument)
        if isinstance(annotation, type) and issubclass(annotation, pydantic.BaseModel) and annotation not in seen:
            seen.add(annotation)
            for name, field in annotation.model_fields.items():
                if _contains_dict(field.annotation):
                    fields.add(f"{annotation.__name__}.{name}")
                _walk(field.annotation)

    for handler in pyfltr.cli.mcp_server.TOOL_HANDLERS.values():
        _walk(inspect.signature(handler, eval_str=True).return_annotation)
    return fields


def test_dict_fields_have_key_policies() -> None:
    """辞書型の応答項目は、キーの出所と限定方法を記録した表と一致する。"""
    actual = _collect_dict_fields()
    assert actual == set(_DICT_FIELD_POLICIES), (
        "辞書型のMCP応答項目と、キーの限定方法の表が一致しません。"
        f"追加: {sorted(actual - set(_DICT_FIELD_POLICIES))}、表にだけある項目: {sorted(set(_DICT_FIELD_POLICIES) - actual)}。"
        "辞書を応答へ渡す場合は返すキーを許可集合で限定し、_DICT_FIELD_POLICIESへ方法を書いてください。"
    )


@pytest.mark.asyncio
async def test_command_info_keys_have_recorded_purposes() -> None:
    """`command_info`の`info`の全キーが用途の表に記録され、最上位と重複するキーを含まない。"""
    config = pyfltr.config.config.load_config()
    keys: set[str] = set()
    for command in config.command_names:
        result = await pyfltr.cli.mcp_server.tool_command_info(command)
        keys.update(result.info)
    unknown = sorted(keys - set(_COMMAND_INFO_KEY_PURPOSES))
    assert not unknown, (
        f"command_infoのinfoに用途未記録のキーがあります: {unknown}。"
        "解決失敗の切り分けに使う用途を_COMMAND_INFO_KEY_PURPOSESへ書くか、応答から除いてください。"
    )
    assert not keys & pyfltr.cli.mcp_server.COMMAND_INFO_EXCLUDED_KEYS


def test_show_run_meta_keys_exclude_internal_values() -> None:
    """`show_run`の`meta`へ返す保存項目に、重複・保存形式の印・実行環境の内部値を含めない。"""
    keys = set(pyfltr.cli.mcp_runs_tools.SHOW_RUN_META_KEYS)
    assert not keys & {"run_id", "slot_waits", "diagnostic_paths", "python", "executable", "platform"}


@pytest.mark.asyncio
async def test_grep_grouped_matches_drop_unrecorded_keys(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """grepの縮約出力の一致へ生成側が用途未記録のキーを加えても、応答へ配送しない。"""
    (tmp_path / "many.txt").write_text("".join(f"foo {index}\n" for index in range(300)), encoding="utf-8")
    (tmp_path / "one.txt").write_text("foo one\n", encoding="utf-8")
    original = pyfltr.grep_.jsonl_records.match_payload

    def _with_internal_key(*args: typing.Any, **kwargs: typing.Any) -> dict[str, typing.Any]:
        return {**original(*args, **kwargs), "internal_debug": 1}

    monkeypatch.setattr(pyfltr.grep_.jsonl_records, "match_payload", _with_internal_key)
    result = await pyfltr.cli.mcp_grep_tools.tool_grep(paths=[str(tmp_path)], pattern="foo", no_gitignore=True, no_exclude=True)
    assert result.output_mode == "mixed"
    grouped = [match for file_result in result.file_results for match in file_result.matches]
    assert grouped
    assert all("internal_debug" not in match and "line" in match and "file" not in match for match in grouped)
    assert all("internal_debug" not in match.model_dump() for match in result.matches)
