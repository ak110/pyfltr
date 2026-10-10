import collections.abc
import json
import logging
import os
import pathlib
import subprocess
import sys
import typing

import pytest

import pyfltr.cli.main
import pyfltr.cli.output_format
import pyfltr.cli.overrides
import pyfltr.cli.pipeline
import pyfltr.command.core_
import pyfltr.command.dispatcher
import pyfltr.command.precommit_guidance
import pyfltr.command.slow_tests
import pyfltr.config.config
import pyfltr.config.editing
import pyfltr.diagnostics
import pyfltr.output.logging_
import pyfltr.output.render
import pyfltr.parsing.entry
import pyfltr.run_options
import pyfltr.state.archive
import pyfltr.state.cache
import pyfltr.warnings_
from tests import conftest as _testconf
from tests.conftest import make_command_result as _make_result
from tests.conftest import make_execution_context as _make_ctx


def test_cli_command_info_check_modes(capsys: pytest.CaptureFixture[str], mocker) -> None:
    """CLIの`--check`指定だけが実行ファイルを起動して確認する。"""
    run_mock = mocker.patch("subprocess.run", side_effect=AssertionError("確認なしで外部プロセスを起動した"))
    assert pyfltr.cli.main.run(["command-info", "typos", "--output-format=json"]) == 0
    mocker.stop(run_mock)
    without_check = json.loads(capsys.readouterr().out)
    assert without_check["command"] == "typos"
    assert "check_passed" not in without_check

    assert pyfltr.cli.main.run(["command-info", "typos", "--output-format=json", "--check"]) == 0
    checked = json.loads(capsys.readouterr().out)
    assert checked["command"] == "typos"
    assert checked["check_passed"] is True
    assert checked["check_installed_version"].startswith("typos-cli ")


@pytest.fixture(name="text_logs")
def _text_logs() -> collections.abc.Iterator[list[str]]:
    """`pyfltr.output.logging_.text_logger`の`info`出力を収集するフィクスチャ。

    `propagate=False`のため caplog / capsys の sys.stdout 差し替えでは捕捉できない
    （pytest capture は setup 段階の sys.stdout 参照と実行時の sys.stdout が一致しない）。
    text_logger に専用 ListHandler を直接追加し、テスト終了時に取り外す。

    Returns:
        現在のテスト内で text_logger が記録したメッセージ文字列のリスト。
        `logging.Handler.format` を通すことで `%` フォーマット差分を吸収する。
    """
    messages: list[str] = []

    class _ListHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            messages.append(self.format(record))

    handler = _ListHandler(level=logging.DEBUG)
    handler.setFormatter(logging.Formatter("%(message)s"))
    pyfltr.output.logging_.text_logger.addHandler(handler)
    original_level = pyfltr.output.logging_.text_logger.level
    pyfltr.output.logging_.text_logger.setLevel(logging.DEBUG)
    try:
        yield messages
    finally:
        pyfltr.output.logging_.text_logger.removeHandler(handler)
        pyfltr.output.logging_.text_logger.setLevel(original_level)


def test_write_log(text_logs):
    """write_logの出力確認。"""
    result = pyfltr.command.core_.CommandResult(
        command="pytest",
        command_type="tester",
        commandline=["pytest", "test.py"],
        returncode=0,
        formatter_failed=False,
        files=3,
        output="ok",
        elapsed=1.5,
    )
    pyfltr.output.render.write_log(result)
    text = "\n".join(text_logs)
    assert "pytest" in text
    assert "returncode: 0" in text


def test_write_log_failed(text_logs):
    """write_logの失敗時の出力確認。"""
    result = pyfltr.command.core_.CommandResult(
        command="pytest",
        command_type="tester",
        commandline=["pytest", "test.py"],
        returncode=1,
        files=2,
        output="FAILED",
        elapsed=0.8,
    )
    pyfltr.output.render.write_log(result)
    # 失敗時は@マークが使われる
    assert "@ returncode: 1" in "\n".join(text_logs)


@pytest.mark.parametrize("returncode", [0, 1])
def test_write_log_shows_slow_tests_on_success_and_failure(text_logs, returncode: int) -> None:
    """端末表示は成功時と失敗時の双方で遅いテスト一覧を出力する。"""
    slow_test = pyfltr.command.slow_tests.SlowTest("tests/a_test.py::test_slow", "call", 1.5)
    result = pyfltr.command.core_.CommandResult(
        command="pytest",
        command_type="tester",
        commandline=["pytest"],
        returncode=returncode,
        files=1,
        output="FAILED" if returncode else "1 passed in 1.50s",
        elapsed=1.5,
        slow_tests=[slow_test],
    )
    pyfltr.output.render.write_log(result)
    assert "1.50s call tests/a_test.py::test_slow" in "\n".join(text_logs)


def test_write_log_tester_with_errors_also_writes_raw_output(text_logs):
    """テスター失敗時は診断一覧に加えて生出力も併記される。"""
    error = pyfltr.diagnostics.ErrorLocation(
        file="tests/a_test.py", line=0, col=None, command="pytest", message="test_a: crashed"
    )
    result = pyfltr.command.core_.CommandResult(
        command="pytest",
        command_type="tester",
        commandline=["pytest"],
        returncode=1,
        files=1,
        output="RAW PYTEST OUTPUT crash detail",
        elapsed=1.0,
        errors=[error],
    )
    pyfltr.output.render.write_log(result)
    text = "\n".join(text_logs)
    assert "test_a: crashed" in text
    assert "RAW PYTEST OUTPUT crash detail" in text


def test_write_log_tester_github_annotations_wraps_raw_output_with_stop_commands(text_logs):
    """GA注釈モードでは生出力を::stop-commands::で囲みワークフローコマンド誤爆を防ぐ。"""
    error = pyfltr.diagnostics.ErrorLocation(
        file="tests/a_test.py", line=0, col=None, command="pytest", message="test_a: crashed"
    )
    result = pyfltr.command.core_.CommandResult(
        command="pytest",
        command_type="tester",
        commandline=["pytest"],
        returncode=1,
        files=1,
        output="::error:: this looks like a workflow command",
        elapsed=1.0,
        errors=[error],
    )
    pyfltr.output.render.write_log(result, use_github_annotations=True)
    text = "\n".join(text_logs)
    assert "::stop-commands::" in text


def _make_pipeline_args(*, stream: bool) -> pyfltr.run_options.RunOptions:
    """run_pipeline呼び出し用の最小Namespaceを生成する。"""
    return pyfltr.run_options.RunOptions.from_values(
        {
            "output_format": "text",
            "output_file": None,
            "format_source": None,
            "stream": stream,
            "no_clear": True,
            "no_ui": True,
            "ui": False,
            "targets": [],
            "exit_zero_even_if_formatted": True,
            "include_fix_stage": False,
            "fail_fast": False,
            "jobs": None,
            "no_exclude": False,
            "no_gitignore": False,
            "allow_external_paths": False,
            "human_readable": False,
            "shuffle": False,
            "verbose": False,
            "ci": False,
            "only_failed": False,
            "from_run": None,
            "changed_since": None,
            "no_archive": True,
            "no_cache": True,
        }
    )


def test_run_one_command_stream_mode_writes_detail_log(mocker, text_logs, tmp_path, monkeypatch):
    """stream=Trueのとき詳細ログを即時出力すること（run_pipeline経由）。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[tool.pyfltr]\nmypy = true\n", encoding="utf-8")
    config = pyfltr.config.config.load_config()

    result = _make_result("mypy", returncode=0, output="ok")
    mocker.patch("pyfltr.command.dispatcher.execute_command", return_value=result)
    # configure_text_output はハンドラーを差し替えるため text_logs フィクスチャのリスナーが
    # 除去される。無操作化してフィクスチャのハンドラーを維持する。
    mocker.patch("pyfltr.output.logging_.configure_text_output")
    mocker.patch("pyfltr.output.logging_.configure_structured_output")

    pyfltr.cli.pipeline.run_pipeline(_make_pipeline_args(stream=True), ["mypy"], config)

    text = "\n".join(text_logs)
    assert "mypy 実行中です..." in text
    # 成功時はエラーなし・生出力なしのためoutputは表示されない
    assert "* returncode: 0" in text


def test_run_one_command_buffer_mode_shows_only_progress(mocker, text_logs, tmp_path, monkeypatch):
    """stream=Falseのとき実行中に1行進捗のみ出力し詳細はon_finishにまとめること（run_pipeline経由）。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[tool.pyfltr]\nmypy = true\n", encoding="utf-8")
    config = pyfltr.config.config.load_config()

    result = _make_result("mypy", returncode=0, output="ok")
    mocker.patch("pyfltr.command.dispatcher.execute_command", return_value=result)
    # configure_text_output はハンドラーを差し替えるため text_logs フィクスチャのリスナーが
    # 除去される。無操作化してフィクスチャのハンドラーを維持する。
    mocker.patch("pyfltr.output.logging_.configure_text_output")
    mocker.patch("pyfltr.output.logging_.configure_structured_output")

    pyfltr.cli.pipeline.run_pipeline(_make_pipeline_args(stream=False), ["mypy"], config)

    text = "\n".join(text_logs)
    assert "mypy 実行中です..." in text
    # buffer modeでは各コマンド完了時に1行進捗が出る
    assert "mypy 完了" in text
    # buffer modeではon_finishでまとめて詳細とsummaryが出る
    assert "summary" in text


def test_render_results_orders_success_failed_summary(text_logs):
    """成功コマンド → 失敗コマンド → summary の順で出力されること。"""
    config = pyfltr.config.config.create_default_config()
    # 失敗コマンドはerrorsが空のため、生出力がフォールバック表示される
    results = [
        _make_result("mypy", returncode=1, output="MYPY_ERROR"),
        _make_result("ruff-format", returncode=0, command_type="formatter"),
        _make_result("pylint", returncode=0),
    ]

    pyfltr.output.render.render_results(results, config, include_details=True)

    text = "\n".join(text_logs)
    # 成功コマンドのヘッダーが先頭に位置する
    ruff_format_pos = text.index("ruff-format")
    pylint_pos = text.index("pylint")
    # 失敗コマンドの生出力がフォールバック表示される
    mypy_pos = text.index("MYPY_ERROR")
    # summary が末尾に位置する
    summary_pos = text.index("summary")

    assert ruff_format_pos < mypy_pos
    assert pylint_pos < mypy_pos
    assert mypy_pos < summary_pos


def test_render_results_include_details_false_writes_only_summary(text_logs):
    """include_details=Falseのときはsummaryのみで詳細ログは出力しない。"""
    config = pyfltr.config.config.create_default_config()
    results = [_make_result("mypy", returncode=1, output="MYPY_ERROR")]

    pyfltr.output.render.render_results(results, config, include_details=False)
    text = "\n".join(text_logs)
    assert "summary" in text
    assert "MYPY_ERROR" not in text


@pytest.mark.parametrize("tool", ["pre-commit", "prek"])
def test_render_results_writes_warnings_section_before_summary(text_logs, tool: str):
    """warnings引数が渡されるとsummary直前にwarningsセクションが出る。"""
    config = pyfltr.config.config.create_default_config()
    results = [_make_result("mypy", returncode=0)]
    warning_message = f"{tool} 設定ファイル不在"
    warnings = [{"source": "config", "message": warning_message}]

    pyfltr.output.render.render_results(results, config, include_details=True, warnings=warnings)

    text = "\n".join(text_logs)
    warning_pos = text.index(warning_message)
    summary_pos = text.index("summary")
    assert warning_pos < summary_pos
    assert "[config]" in text


def test_run_text_output_shows_warning_hint_in_section_and_stderr(
    _isolated_target: pathlib.Path, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    """テキスト表示では、hintを持つ警告の対処がwarnings節と標準エラーの双方に出る。"""
    returncode = pyfltr.cli.main.run(
        [
            "run",
            "--output-format=text",
            "--no-clear",
            "--no-archive",
            "--commands=ruff-format",
            "--disable=ruff-format",
            "--work-dir",
            str(_isolated_target),
            str(_isolated_target / "sample.py"),
        ]
    )

    assert returncode == 0
    out = capsys.readouterr().out
    section = out[out.index("-- warnings") :]
    assert "[commands]" in section
    assert "対処: --enable=ruff-format" in section
    # stderrへはroot logger経由で出力されるため、ログ記録で対処の有無を確かめる
    assert "対処: --enable=ruff-format" in caplog.text


def test_run_archive_and_cache_init_failures_guide_actions(
    _isolated_target: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """アーカイブ・キャッシュを初期化できない場合は、続行した結果と権限確認・無効化の指定を案内する。"""

    def _raise(*_args: object, **_kwargs: object) -> typing.NoReturn:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(pyfltr.state.archive.ArchiveStore, "start_run", _raise)
    monkeypatch.setattr(pyfltr.state.cache.CacheStore, "cleanup", _raise)
    pyfltr.cli.main.run(
        [
            "run",
            "--output-format=jsonl",
            "--commands=ruff-format",
            "--disable=ruff-format",
            "--work-dir",
            str(_isolated_target),
            str(_isolated_target / "sample.py"),
        ]
    )
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    warnings = {r["source"]: r for r in records if r["kind"] == "warning" and r["source"] in ("archive", "cache")}
    assert "アーカイブ無しで続行しました" in warnings["archive"]["msg"]
    assert "`--no-archive`" in warnings["archive"]["hint"]
    assert "キャッシュ無しで続行しました" in warnings["cache"]["msg"]
    assert "`--no-cache`" in warnings["cache"]["hint"]


def test_render_results_skips_warnings_section_when_empty(text_logs):
    """warningsが空のときはwarnings見出しを出力しない。"""
    config = pyfltr.config.config.create_default_config()
    results = [_make_result("mypy", returncode=0)]

    pyfltr.output.render.render_results(results, config, include_details=True, warnings=[])

    # warningsセクションは出力されない（summary直前の見出しだけを検証するのは困難なため、
    # [source]形式のエントリ行が無いことで代替する）
    assert "[config]" not in "\n".join(text_logs)


def test_render_results_writes_missing_targets_section_before_summary(text_logs):
    """`missing_targets`蓄積時はsummary直前に専用ブロックが出力される。

    `fully-excluded-files`との並びはmissing-targetsを先に出力する前提で固定する。
    両者は原因が異なる（不在 vs exclude設定）ため独立ブロックで識別可能とする。
    """
    config = pyfltr.config.config.create_default_config()
    results = [_make_result("mypy", returncode=0)]
    pyfltr.warnings_.add_filtered_direct_file("does_not_exist.py", reason="missing")
    pyfltr.warnings_.add_filtered_direct_file("excluded.py", reason="excluded")

    pyfltr.output.render.render_results(results, config, include_details=True)

    text = "\n".join(text_logs)
    missing_block_pos = text.index("missing-targets")
    missing_path_pos = text.index("does_not_exist.py")
    excluded_block_pos = text.index("fully-excluded-files")
    summary_pos = text.index("summary")
    assert missing_block_pos < missing_path_pos < excluded_block_pos < summary_pos


def test_render_results_skips_missing_targets_section_when_empty(text_logs):
    """`missing_targets`が空のときは専用ブロックを出力しない。"""
    config = pyfltr.config.config.create_default_config()
    results = [_make_result("mypy", returncode=0)]

    pyfltr.output.render.render_results(results, config, include_details=True)

    assert "missing-targets" not in "\n".join(text_logs)


def test_run_commands_with_cli_fail_fast_aborts_remaining_fixers(mocker):
    """--fail-fast発動時、fixステージのエラーで後続のformatter/linterをskipped化する。"""
    config = pyfltr.config.config.create_default_config()
    config.values["ruff-check"] = True
    config.values["ruff-format"] = True
    config.values["mypy"] = True
    # fixステージでruff-checkがfailedを返る想定
    fix_fail = _make_result("ruff-check", returncode=1, command_type="linter")
    mocker.patch("pyfltr.command.dispatcher.execute_command", return_value=fix_fail)
    mock_args = mocker.MagicMock()

    base_ctx = _make_ctx(config, []).base
    results = pyfltr.cli.pipeline.run_commands_with_cli(
        ["ruff-check", "ruff-format", "mypy"],
        mock_args,
        base_ctx,
        per_command_log=False,
        include_fix_stage=True,
        fail_fast=True,
    )
    # 通常ステージはスキップされ、ruff-formatとmypyがskippedで蓄積される
    statuses = {r.command: r.status for r in results}
    assert statuses.get("ruff-format") == "skipped"
    assert statuses.get("mypy") == "skipped"


def test_run_commands_with_cli_without_fail_fast_continues(mocker):
    """fail_fast=Falseなら1ツール失敗でも後続が実行される。"""
    config = pyfltr.config.config.create_default_config()
    config.values["ruff-format"] = True
    config.values["mypy"] = True
    fail_result = _make_result("ruff-format", returncode=1, command_type="formatter", formatter_failed=True)
    success = _make_result("mypy", returncode=0)

    def _fake_execute(command, *_args, **_kwargs):
        return fail_result if command == "ruff-format" else success

    mocker.patch("pyfltr.command.dispatcher.execute_command", side_effect=_fake_execute)
    mock_args = mocker.MagicMock()

    base_ctx = _make_ctx(config, []).base
    results = pyfltr.cli.pipeline.run_commands_with_cli(
        ["ruff-format", "mypy"],
        mock_args,
        base_ctx,
        per_command_log=False,
        include_fix_stage=False,
        fail_fast=False,
    )
    commands = [r.command for r in results]
    assert "mypy" in commands
    assert "ruff-format" in commands
    assert not any(r.status == "skipped" for r in results)


# --- `--commands` 未知値経路のparser.error整形 ---


@pytest.mark.parametrize(
    "command,expect_suggestion",
    [
        # typoしきい値内 → サジェストあり
        ("pylit", True),
        # カスタムコマンド名typo（ruff-checkの近接）
        ("ruff-cheack", True),
        # しきい値外 → サジェストなし
        ("totally-unrelated", False),
    ],
)
def test_run_commands_unknown_emits_suggestion(
    monkeypatch,
    tmp_path,
    capsys,
    command: str,
    expect_suggestion: bool,
) -> None:
    """`pyfltr run --commands=<typo>`経路でparser.errorに近接候補が含まれる。"""
    (tmp_path / "pyproject.toml").write_text("[tool.pyfltr]\n", encoding="utf-8")
    (tmp_path / "x.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit):
        pyfltr.cli.main.run(["run", f"--commands={command}", "x.py"])
    err = capsys.readouterr().err
    assert "コマンドが見つかりません" in err
    if expect_suggestion:
        assert "もしかして:" in err
    else:
        assert "もしかして:" not in err


def _make_overrides_args(
    *,
    enable: list[str] | None = None,
    disable: list[str] | None = None,
) -> pyfltr.run_options.RunOptions:
    """`apply_cli_overrides` 呼び出し用の最小Namespaceを生成する。"""
    return pyfltr.run_options.RunOptions.from_values(
        {
            "jobs": None,
            "no_exclude": False,
            "no_gitignore": False,
            "allow_external_paths": False,
            "human_readable": False,
            "enable": enable,
            "disable": disable,
        }
    )


def test_apply_cli_overrides_enable_single():
    """`--enable=mypy` で `mypy` が有効化される。"""
    config = pyfltr.config.config.create_default_config()
    config.values["mypy"] = False
    pyfltr.cli.overrides.apply_cli_overrides(config, _make_overrides_args(enable=["mypy"]))
    assert config.values["mypy"] is True


def test_apply_cli_overrides_enable_comma_separated():
    """`--enable=mypy,pyright` で両方が有効化される。"""
    config = pyfltr.config.config.create_default_config()
    config.values["mypy"] = False
    config.values["pyright"] = False
    pyfltr.cli.overrides.apply_cli_overrides(config, _make_overrides_args(enable=["mypy,pyright"]))
    assert config.values["mypy"] is True
    assert config.values["pyright"] is True


def test_apply_cli_overrides_enable_multi_specified():
    """`--enable=mypy --enable=pyright` で両方が有効化される。"""
    config = pyfltr.config.config.create_default_config()
    config.values["mypy"] = False
    config.values["pyright"] = False
    pyfltr.cli.overrides.apply_cli_overrides(config, _make_overrides_args(enable=["mypy", "pyright"]))
    assert config.values["mypy"] is True
    assert config.values["pyright"] is True


def test_apply_cli_overrides_disable_single():
    """`--disable=ruff-check` で `ruff-check` が無効化される。"""
    config = pyfltr.config.config.create_default_config()
    config.values["ruff-check"] = True
    pyfltr.cli.overrides.apply_cli_overrides(config, _make_overrides_args(disable=["ruff-check"]))
    assert config.values["ruff-check"] is False


def test_apply_cli_overrides_enable_takes_precedence_over_disable():
    """`--enable=mypy --disable=mypy` で `enable` が優先される。"""
    config = pyfltr.config.config.create_default_config()
    config.values["mypy"] = False
    pyfltr.cli.overrides.apply_cli_overrides(config, _make_overrides_args(enable=["mypy"], disable=["mypy"]))
    assert config.values["mypy"] is True


def test_apply_cli_overrides_unknown_command_emits_warning():
    """未知コマンド名指定時は警告し config は変更しない。"""
    config = pyfltr.config.config.create_default_config()
    before = dict(config.values)
    pyfltr.cli.overrides.apply_cli_overrides(config, _make_overrides_args(enable=["nonexistent"]))
    assert config.values == before
    warnings = pyfltr.warnings_.collected_warnings()
    unknown = [w for w in warnings if "nonexistent" in w["message"]]
    assert unknown
    # 無視した結果に加えて、登録済みコマンド名を確認する手段を案内する
    assert "無視しました" in unknown[0]["message"]
    assert "pyfltr config list --all" in unknown[0]["hint"]


def test_apply_cli_overrides_none_leaves_config_unchanged():
    """`--enable`・`--disable` 未指定時は config を変更しない。"""
    config = pyfltr.config.config.create_default_config()
    before = dict(config.values)
    pyfltr.cli.overrides.apply_cli_overrides(config, _make_overrides_args())
    assert config.values == before


class TestConfigSubcommand:
    """`pyfltr config`サブコマンドの統合テスト。

    `_isolate_global_config`fixture（autouse）で`PYFLTR_GLOBAL_CONFIG`は
    既にtmp配下のダミーパスへ固定されているため、`--global`時はそのパスが
    対象となる。project側は`monkeypatch.chdir(tmp_path)`でcwd配下の
    `pyproject.toml`が解決されるようにする。
    """

    def test_config_get_existing_key(self, monkeypatch, tmp_path, capsys) -> None:
        """project側で設定した値が`config get`で返る。"""
        (tmp_path / "pyproject.toml").write_text("[tool.pyfltr]\narchive-max-age-days = 7\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        rc = pyfltr.cli.main.run(["config", "get", "archive-max-age-days"])
        assert rc == 0
        assert capsys.readouterr().out.strip() == "7"

    def test_config_get_default_value(self, monkeypatch, tmp_path, capsys) -> None:
        """未設定キーは`DEFAULT_CONFIG`の既定値が返る。"""
        (tmp_path / "pyproject.toml").write_text("[tool.pyfltr]\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        rc = pyfltr.cli.main.run(["config", "get", "archive-max-age-days"])
        assert rc == 0
        # DEFAULT_CONFIGは30
        assert capsys.readouterr().out.strip() == "30"

    def test_config_get_unknown_key_errors(self, monkeypatch, tmp_path, capsys) -> None:
        """未知キーはexit 1。"""
        (tmp_path / "pyproject.toml").write_text("[tool.pyfltr]\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        rc = pyfltr.cli.main.run(["config", "get", "unknown-key"])
        assert rc == 1
        assert "unknown-key" in capsys.readouterr().err

    def test_config_set_creates_pyproject_section(self, monkeypatch, tmp_path) -> None:
        """既存pyproject.tomlに対してsetが書き込み成功する（[tool.pyfltr]セクションが無くても）。"""
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\n', encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        rc = pyfltr.cli.main.run(["config", "set", "archive-max-age-days", "5"])
        assert rc == 0
        text = (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
        assert "[tool.pyfltr]" in text
        assert "archive-max-age-days = 5" in text

    def test_config_set_preserves_comments(self, monkeypatch, tmp_path) -> None:
        """既存pyproject.tomlのコメントが保持される（tomlkit効果の確認）。"""
        original = '[project]\nname = "demo"  # 重要なコメント\n\n[tool.pyfltr]\n# pyfltrのコメント\npreset = "latest"\n'
        (tmp_path / "pyproject.toml").write_text(original, encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        rc = pyfltr.cli.main.run(["config", "set", "archive-max-age-days", "10"])
        assert rc == 0
        text = (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
        assert "# 重要なコメント" in text
        assert "# pyfltrのコメント" in text
        assert "archive-max-age-days = 10" in text

    def test_config_set_pyproject_missing_errors(self, monkeypatch, tmp_path, capsys) -> None:
        """pyproject不在ディレクトリでのsetはエラー終了。`--global`併用案内を含む。"""
        # tmp_pathにpyproject.tomlを生成しない
        monkeypatch.chdir(tmp_path)
        rc = pyfltr.cli.main.run(["config", "set", "archive-max-age-days", "5"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "pyproject.toml" in err
        assert "--global" in err
        assert "プロジェクトのルートで実行する" in err

    def test_config_set_global_creates_file(self, monkeypatch, tmp_path) -> None:
        """`--global`指定時にglobal config.tomlが自動作成される。"""
        global_path = pathlib.Path(_get_global_config_env())
        # 既に存在しないことを確認
        assert not global_path.exists()
        monkeypatch.chdir(tmp_path)
        rc = pyfltr.cli.main.run(["config", "set", "--global", "archive-max-age-days", "5"])
        assert rc == 0
        assert global_path.exists()
        text = global_path.read_text(encoding="utf-8")
        assert "[tool.pyfltr]" in text
        assert "archive-max-age-days = 5" in text

    def test_config_set_warning_archive_in_project(self, monkeypatch, tmp_path) -> None:
        """archive-max-age-daysをproject側にsetすると警告が蓄積される。"""
        (tmp_path / "pyproject.toml").write_text("[tool.pyfltr]\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        rc = pyfltr.cli.main.run(["config", "set", "archive-max-age-days", "5"])
        assert rc == 0
        assert _count_config_warnings("archive-max-age-days") == 1

    def test_config_set_warning_normal_in_global(self, monkeypatch, tmp_path) -> None:
        """js-runnerをglobal側にsetすると警告（archive/cache以外はproject優先）。"""
        monkeypatch.chdir(tmp_path)
        rc = pyfltr.cli.main.run(["config", "set", "--global", "js-runner", "npm"])
        assert rc == 0
        assert _count_config_warnings("js-runner") == 1

    def test_config_delete_existing_key(self, monkeypatch, tmp_path, capsys) -> None:
        """存在キーをdeleteで削除し、その後getすると既定値が返る。"""
        (tmp_path / "pyproject.toml").write_text("[tool.pyfltr]\narchive-max-age-days = 5\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        rc = pyfltr.cli.main.run(["config", "delete", "archive-max-age-days"])
        assert rc == 0
        capsys.readouterr()
        rc = pyfltr.cli.main.run(["config", "get", "archive-max-age-days"])
        assert rc == 0
        assert capsys.readouterr().out.strip() == "30"

    def test_config_delete_missing_key(self, monkeypatch, tmp_path, capsys) -> None:
        """存在しないキーのdeleteはexit 0で終了。"""
        (tmp_path / "pyproject.toml").write_text("[tool.pyfltr]\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        rc = pyfltr.cli.main.run(["config", "delete", "archive-max-age-days"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "archive-max-age-days" in out

    def test_config_list_text_format(self, monkeypatch, tmp_path, capsys) -> None:
        """textフォーマットで`key = value`形式が出力される。"""
        (tmp_path / "pyproject.toml").write_text(
            '[tool.pyfltr]\narchive-max-age-days = 5\njs-runner = "pnpm"\n', encoding="utf-8"
        )
        monkeypatch.chdir(tmp_path)
        rc = pyfltr.cli.main.run(["config", "list"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "archive-max-age-days = 5" in out
        assert "js-runner = pnpm" in out

    def test_config_list_json_format(self, monkeypatch, tmp_path, capsys) -> None:
        """jsonフォーマットで`{"values": ...}`が出力される。"""
        (tmp_path / "pyproject.toml").write_text("[tool.pyfltr]\narchive-max-age-days = 5\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        rc = pyfltr.cli.main.run(["config", "list", "--output-format", "json"])
        assert rc == 0
        out = capsys.readouterr().out.strip()
        data = json.loads(out)
        assert data == {"values": {"archive-max-age-days": 5}}

    @pytest.mark.parametrize("env_name", pyfltr.cli.output_format.AGENT_INDICATOR_ENVS)
    def test_config_list_agent_indicator_jsonl(self, env_name, monkeypatch, tmp_path, capsys) -> None:
        """エージェント検出変数のいずれかが設定されていれば、`config list`は--output-format未指定でもJSONLを出力する。"""
        (tmp_path / "pyproject.toml").write_text("[tool.pyfltr]\narchive-max-age-days = 5\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv(env_name, "1")
        rc = pyfltr.cli.main.run(["config", "list"])
        assert rc == 0
        lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
        assert len(lines) == 1
        assert json.loads(lines[0]) == {"key": "archive-max-age-days", "value": 5}

    def test_config_list_all_text_includes_defaults(self, monkeypatch, tmp_path, capsys) -> None:
        """`--all` text出力で既定値行に`(default)`が付き、明示値行には付かない。"""
        (tmp_path / "pyproject.toml").write_text("[tool.pyfltr]\narchive-max-age-days = 5\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        rc = pyfltr.cli.main.run(["config", "list", "--all"])
        assert rc == 0
        out = capsys.readouterr().out
        lines = [line for line in out.splitlines() if line.strip()]
        # 明示値: (default)なし
        assert "archive-max-age-days = 5" in lines
        # 既定値の例: (default)付きで出力される（DEFAULT_CONFIGにあるキー）
        default_lines = [line for line in lines if line.endswith(" (default)")]
        assert len(default_lines) > 0
        # キー昇順
        keys = [line.split(" = ", 1)[0] for line in lines]
        assert keys == sorted(keys)

    def test_config_list_all_json_marks_default_per_key(self, monkeypatch, tmp_path, capsys) -> None:
        """`--all` json出力で各キーに`value`と`default`の2フィールドが付与される。"""
        (tmp_path / "pyproject.toml").write_text("[tool.pyfltr]\narchive-max-age-days = 5\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        rc = pyfltr.cli.main.run(["config", "list", "--all", "--output-format", "json"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out.strip())
        assert "values" in data
        values = data["values"]
        # 明示値
        assert values["archive-max-age-days"] == {"value": 5, "default": False}
        # 既定値の例（DEFAULT_CONFIGに存在し未明示のキー）
        defaults_marked = [v for v in values.values() if v["default"]]
        assert len(defaults_marked) > 0

    def test_config_list_all_jsonl_appends_default_field(self, monkeypatch, tmp_path, capsys) -> None:
        """`--all` jsonl出力で各行に`default`フィールドが追加される。"""
        (tmp_path / "pyproject.toml").write_text("[tool.pyfltr]\narchive-max-age-days = 5\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        rc = pyfltr.cli.main.run(["config", "list", "--all", "--output-format", "jsonl"])
        assert rc == 0
        lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
        # 明示値行
        explicit = [rec for rec in lines if rec["key"] == "archive-max-age-days"]
        assert explicit == [{"key": "archive-max-age-days", "value": 5, "default": False}]
        # 既定値行が含まれる
        assert any(rec["default"] for rec in lines)
        # キー昇順
        keys = [rec["key"] for rec in lines]
        assert keys == sorted(keys)

    def test_config_list_all_empty_pyproject_marks_all_default(self, monkeypatch, tmp_path, capsys) -> None:
        """全キー既定（明示値なし）の場合、全行に`(default)`が付く。"""
        (tmp_path / "pyproject.toml").write_text("[tool.pyfltr]\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        rc = pyfltr.cli.main.run(["config", "list", "--all"])
        assert rc == 0
        lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
        assert lines
        assert all(line.endswith(" (default)") for line in lines)

    def test_config_list_all_includes_colloquial_dynamic_defaults(self, monkeypatch, tmp_path, capsys) -> None:
        """`config list --all`は口語表現検査の動的設定キーと既定値を返す。"""
        (tmp_path / "pyproject.toml").write_text("[tool.pyfltr]\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        rc = pyfltr.cli.main.run(["config", "list", "--all", "--output-format", "json"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out.strip())
        values = data["values"]
        assert values["colloquial-check-exclude"] == {
            "value": [],
            "default": True,
        }
        assert values["colloquial-check-targets"] == {
            "value": "*",
            "default": True,
        }

    @pytest.mark.parametrize(
        ("key", "expected"),
        [
            ("colloquial-check-exclude", ""),
            ("colloquial-check-targets", "*"),
        ],
    )
    def test_config_get_colloquial_dynamic_default(self, monkeypatch, tmp_path, capsys, key: str, expected: str) -> None:
        """口語表現検査の動的設定キーは未設定でも既定値を取得できる。"""
        (tmp_path / "pyproject.toml").write_text("[tool.pyfltr]\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        rc = pyfltr.cli.main.run(["config", "get", key])
        assert rc == 0
        assert capsys.readouterr().out.strip() == expected

    @pytest.mark.parametrize(
        ("key", "value", "default"),
        [
            ("colloquial-check-exclude", "docs/generated.md", ""),
            ("colloquial-check-targets", "*.md", "*"),
        ],
    )
    def test_config_set_get_delete_colloquial_dynamic_key(
        self, monkeypatch, tmp_path, capsys, key: str, value: str, default: str
    ) -> None:
        """口語表現検査の動的設定キーは設定・取得・削除後の既定値復帰が一貫して動作する。"""
        (tmp_path / "pyproject.toml").write_text("[tool.pyfltr]\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        assert pyfltr.cli.main.run(["config", "set", key, value]) == 0
        capsys.readouterr()
        assert pyfltr.cli.main.run(["config", "get", key]) == 0
        assert capsys.readouterr().out.strip() == value
        assert pyfltr.cli.main.run(["config", "delete", key]) == 0
        capsys.readouterr()
        assert pyfltr.cli.main.run(["config", "get", key]) == 0
        assert capsys.readouterr().out.strip() == default

    def test_config_set_unknown_key_errors(self, monkeypatch, tmp_path, capsys) -> None:
        """未知キーへのsetはexit 1。"""
        (tmp_path / "pyproject.toml").write_text("[tool.pyfltr]\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        rc = pyfltr.cli.main.run(["config", "set", "unknown-key", "foo"])
        assert rc == 1
        assert "unknown-key" in capsys.readouterr().err

    def test_config_delete_unknown_key_errors(self, monkeypatch, tmp_path, capsys) -> None:
        """未知キーへのdeleteはexit 1。"""
        (tmp_path / "pyproject.toml").write_text("[tool.pyfltr]\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        rc = pyfltr.cli.main.run(["config", "delete", "unknown-key"])
        assert rc == 1
        assert "unknown-key" in capsys.readouterr().err

    def test_config_unknown_subaction_errors(self, monkeypatch, tmp_path) -> None:
        """`pyfltr config`単独はargparseエラー（required=True）。"""
        monkeypatch.chdir(tmp_path)
        with pytest.raises(SystemExit):
            pyfltr.cli.main.run(["config"])

    @pytest.mark.parametrize(
        "action,key,expect_suggestion",
        [
            # typoしきい値内 → サジェスト候補が並ぶ
            ("get", "python-runer", True),
            ("set", "python-runer", True),
            ("delete", "python-runer", True),
            # しきい値外 → 候補無し
            ("get", "totally-unrelated", False),
            ("set", "totally-unrelated", False),
            ("delete", "totally-unrelated", False),
        ],
    )
    def test_config_unknown_key_suggestion(
        self, monkeypatch, tmp_path, capsys, action: str, key: str, expect_suggestion: bool
    ) -> None:
        """`config get/set/delete`の未知キー文面にサジェスト・一覧確認手段が含まれる。"""
        (tmp_path / "pyproject.toml").write_text("[tool.pyfltr]\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        argv = ["config", action, key]
        if action == "set":
            argv.append("foo")
        rc = pyfltr.cli.main.run(argv)
        assert rc == 1
        err = capsys.readouterr().err
        assert f"`{key}`" in err
        assert "pyfltr config list --all" in err
        if expect_suggestion:
            assert "もしかして:" in err
        else:
            assert "もしかして:" not in err


def _get_global_config_env() -> str:
    """`_isolate_global_config`fixtureが設定したglobal設定パスを返す。"""
    value = os.environ.get("PYFLTR_GLOBAL_CONFIG")
    assert value is not None, "PYFLTR_GLOBAL_CONFIG fixtureが機能していない"
    return value


# conftest.count_config_warningsを再エクスポート（同モジュール内の参照を統一するため）
_count_config_warnings = _testconf.count_config_warnings


@pytest.mark.parametrize(
    ("action", "operation"),
    [
        ("get", "read_config_values"),
        ("set", "set_config_value"),
        ("delete", "delete_config_value"),
        ("list", "read_config_values"),
    ],
)
def test_config_io_error_returns_exit_one(action, operation, monkeypatch, tmp_path, capsys) -> None:
    """読み書きの権限エラーはトレースバックを出力せず、エラーメッセージと終了1を返す。"""
    (tmp_path / "pyproject.toml").write_text("[tool.pyfltr]\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    def deny(*_args, **_kwargs):
        raise PermissionError("設定ファイルの操作が拒否されました")

    monkeypatch.setattr(pyfltr.config.editing, operation, deny)
    argv = ["config", action]
    if action != "list":
        argv.append("js-runner")
    if action == "set":
        argv.append("npm")
    assert pyfltr.cli.main.run(argv) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "設定ファイルの操作が拒否されました\n"


@pytest.mark.parametrize("mode", ["run", "ci"])
def test_success(_isolated_target, mocker, mode):
    proc = subprocess.CompletedProcess(["test"], returncode=0, stdout="test")
    mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)
    returncode = pyfltr.cli.main.run([mode, "--work-dir", str(_isolated_target), str(_isolated_target)])
    assert returncode == 0


@pytest.mark.parametrize("mode", ["run", "ci"])
def test_fail(_isolated_target, mocker, mode):
    proc = subprocess.CompletedProcess(["test"], returncode=-1, stdout="test")
    mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)
    returncode = pyfltr.cli.main.run([mode, "--work-dir", str(_isolated_target), str(_isolated_target)])
    assert returncode == 1


def test_missing_subcommand_errors():
    """サブコマンド未指定時にSystemExitが発生することを確認。"""
    with pytest.raises(SystemExit):
        pyfltr.cli.main.run([])


def test_all_targets_missing_returns_nonzero(tmp_path, mocker):
    """指定パスが全件不在の場合は CLI が非ゼロ終了する。"""
    # 別 tmp_path をcwdとして空のpyproject.tomlを置く（preset無しで純粋に判定経路を確認する）
    (tmp_path / "pyproject.toml").write_text("[tool.pyfltr]\n")
    # subprocess呼び出しは早期exit経路により発生しない想定だが、安全のためモックしておく。
    mocker.patch("pyfltr.command.process.run_subprocess", return_value=subprocess.CompletedProcess(["x"], 0, ""))
    original_cwd = pathlib.Path.cwd()
    try:
        os.chdir(tmp_path)
        returncode = pyfltr.cli.main.run(["run-for-agent", "does_not_exist.py"])
    finally:
        os.chdir(original_cwd)
    assert returncode == 1


def test_partial_missing_targets_continues(tmp_path, mocker):
    """指定パスが部分的に存在する場合は処理を継続して通常の終了コードを返す。"""
    # 既定有効ツール（designmd / lychee）は本テストの対象（*.py）と一致しないため、
    # 「partial missing + 全コマンドskip」の別判定（test_partial_missing_with_all_skipped_returns_nonzero）
    # に巻き込まれないよう明示的に無効化する。
    (tmp_path / "pyproject.toml").write_text("[tool.pyfltr]\ndesignmd = false\nlychee = false\n")
    (tmp_path / "exists.py").write_text("x = 1\n")
    mocker.patch(
        "pyfltr.command.process.run_subprocess",
        return_value=subprocess.CompletedProcess(["x"], 0, ""),
    )
    original_cwd = pathlib.Path.cwd()
    try:
        os.chdir(tmp_path)
        returncode = pyfltr.cli.main.run(["run-for-agent", "exists.py", "missing.py"])
    finally:
        os.chdir(original_cwd)
    # 1件は存在するため、全件不在判定は成立せず処理継続。テスト用subprocessは0返却。
    assert returncode == 0


def test_partial_missing_with_all_skipped_returns_nonzero(tmp_path, mocker):
    """部分不在 + 残ファイルで全コマンドskipの場合は意図しない呼び出しとして非ゼロ終了する。

    指定ファイルが見つからずほぼ何も実行されなかった状況を、`missing_targets`非空 +
    全コマンドskipの組合せで検知する。
    """
    # `.py`対象のPython系ツールだけを有効化する（preset未使用で`*`対象ツールが
    # 混入しないようにする）。残ファイルは拡張子`.txt`で全Python系ツール対象外、
    # missing.pyは不在とすることで全コマンドskippedかつmissing_targets非空の状態を再現する。
    (tmp_path / "pyproject.toml").write_text("[tool.pyfltr]\npylint = true\nmypy = true\n")
    (tmp_path / "note.txt").write_text("hello\n")
    mocker.patch(
        "pyfltr.command.process.run_subprocess",
        return_value=subprocess.CompletedProcess(["x"], 0, ""),
    )
    original_cwd = pathlib.Path.cwd()
    try:
        os.chdir(tmp_path)
        returncode = pyfltr.cli.main.run(["run-for-agent", "note.txt", "missing.py"])
    finally:
        os.chdir(original_cwd)
    assert returncode == 1


def test_work_dir(mocker, tmp_path):
    """--work-dirオプションのテスト。"""
    # preset由来のpytestをpython gate通過で実行対象に含める。
    (tmp_path / "pyproject.toml").write_text('[tool.pyfltr]\npreset = "latest"\npython = true\n')
    proc = subprocess.CompletedProcess(["test"], returncode=0, stdout="test")
    mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)
    original_cwd = pathlib.Path.cwd()
    returncode = pyfltr.cli.main.run(
        ["ci", "--work-dir", str(tmp_path), "--commands=pytest", str(pathlib.Path(__file__).parents[2])]
    )
    assert returncode == 0
    # cwdが復元されていることを確認
    assert pathlib.Path.cwd() == original_cwd


def test_run_auto_includes_fix_stage(mocker):
    """runサブコマンドではfix-args付きのfixステージが自動実行される。"""
    proc = subprocess.CompletedProcess(["test"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    # ruff-checkはfix-args定義済みかつpreset=latestで有効化されている
    returncode = pyfltr.cli.main.run(["run", "--commands=ruff-check", str(pathlib.Path(__file__).parents[2])])
    assert returncode == 0

    # 通常モードの引数リストとfixモードの引数リストが両方実行されていること
    invoked_commandlines = [call.args[0] for call in mock_run.call_args_list if call.args and isinstance(call.args[0], list)]
    fix_calls = [cl for cl in invoked_commandlines if "--fix" in cl]
    assert fix_calls, "fixステージが実行されていない"


def test_no_fix_skips_fix_stage(mocker):
    """--no-fix指定時はfixステージがスキップされる。"""
    proc = subprocess.CompletedProcess(["test"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    returncode = pyfltr.cli.main.run(["run", "--no-fix", "--commands=ruff-check", str(pathlib.Path(__file__).parents[2])])
    assert returncode == 0

    invoked_commandlines = [call.args[0] for call in mock_run.call_args_list if call.args and isinstance(call.args[0], list)]
    fix_calls = [cl for cl in invoked_commandlines if "--fix" in cl]
    assert not fix_calls, "--no-fix指定時にfixステージが実行されている"


def test_ci_does_not_run_fix_stage(mocker):
    """ciサブコマンドではfixステージを実行しない（ファイル書換を避けるため）。"""
    proc = subprocess.CompletedProcess(["test"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    pyfltr.cli.main.run(["ci", "--commands=ruff-check", str(pathlib.Path(__file__).parents[2])])

    invoked_commandlines = [call.args[0] for call in mock_run.call_args_list if call.args and isinstance(call.args[0], list)]
    fix_calls = [cl for cl in invoked_commandlines if "--fix" in cl]
    assert not fix_calls, "ciサブコマンドでfixステージが実行されている"


def test_stream_mode_writes_detail_log_during_run(mocker, capsys):
    """--stream指定時はコマンド完了時に詳細ログが出力される。"""
    # pyfltrルートのpyproject.tomlにはpython=trueが設定されているためmypyは有効
    proc = subprocess.CompletedProcess(["mypy"], returncode=0, stdout="mypy-detail")
    mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    returncode = pyfltr.cli.main.run(["ci", "--no-ui", "--stream", "--commands=mypy", str(pathlib.Path(__file__).parents[2])])
    assert returncode == 0
    captured = capsys.readouterr()
    # 詳細ログに含まれるreturncode行が出力される
    assert "returncode: 0" in captured.out
    # summaryセクションも引き続き出力される
    assert "summary" in captured.out


def test_buffered_mode_is_default(mocker, capsys):
    """既定では成功コマンド詳細→summaryの順でまとめて出力される。"""
    # pyfltrルートのpyproject.tomlにはpython=trueが設定されているためmypyは有効
    proc = subprocess.CompletedProcess(["mypy"], returncode=0, stdout="mypy-detail")
    mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    returncode = pyfltr.cli.main.run(["ci", "--no-ui", "--commands=mypy", str(pathlib.Path(__file__).parents[2])])
    assert returncode == 0
    text = capsys.readouterr().out
    # 詳細ログがsummaryより先に位置する（summaryは末尾）
    assert "summary" in text
    assert "returncode: 0" in text
    assert text.index("returncode: 0") < text.index("summary")


def test_additional_args(mocker):
    """追加引数のテスト。"""
    proc = subprocess.CompletedProcess(["pytest"], returncode=0, stdout="test")
    mock_run = mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    returncode = pyfltr.cli.main.run(
        ["ci", "--commands=pytest", "--pytest-args=--maxfail=5 -v", str(pathlib.Path(__file__).parents[2])]
    )
    assert returncode == 0

    # subprocess.runが呼ばれた引数を確認
    assert mock_run.called
    called_args = mock_run.call_args[0][0]  # 最初の引数（コマンドライン）
    assert "--maxfail=5" in called_args
    assert "-v" in called_args


class TestSubcommandIntegration:
    """サブコマンドの統合テスト。"""

    def test_run_subcommand(self, _isolated_target, mocker):
        """runサブコマンドで--exit-zero-even-if-formattedが暗黙的に有効化される。"""
        proc = subprocess.CompletedProcess(["test"], returncode=0, stdout="test")
        mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)
        returncode = pyfltr.cli.main.run(["run", "--work-dir", str(_isolated_target), str(_isolated_target)])
        assert returncode == 0

    def test_fast_subcommand(self, _isolated_target, mocker):
        """fastサブコマンドで--exit-zero-even-if-formattedと--commands=fastが暗黙的に有効化される。"""
        proc = subprocess.CompletedProcess(["test"], returncode=0, stdout="test")
        mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)
        returncode = pyfltr.cli.main.run(["fast", "--work-dir", str(_isolated_target), str(_isolated_target)])
        assert returncode == 0

    def test_run_for_agent_subcommand(self, _isolated_target, mocker):
        """run-for-agentサブコマンドで--exit-zero-even-if-formattedと--output-format=jsonlが暗黙的に有効化される。"""
        proc = subprocess.CompletedProcess(["test"], returncode=0, stdout="test")
        mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)
        returncode = pyfltr.cli.main.run(["run-for-agent", "--work-dir", str(_isolated_target), str(_isolated_target)])
        assert returncode == 0

    def test_ci_explicit(self, _isolated_target, mocker):
        """明示的なciサブコマンド。"""
        proc = subprocess.CompletedProcess(["test"], returncode=0, stdout="test")
        mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)
        returncode = pyfltr.cli.main.run(["ci", "--work-dir", str(_isolated_target), str(_isolated_target)])
        assert returncode == 0

    def test_run_includes_custom_commands_by_default(self, mocker, tmp_path):
        """`run`サブコマンドでcustom-commandsを--commandsで明示すると実行される。"""
        pyproject = """
[tool.pyfltr]

[tool.pyfltr.custom-commands.my-linter]
type = "linter"
path = "my-linter-exe"
targets = ["*.py"]
pass-filenames = false
"""
        (tmp_path / "pyproject.toml").write_text(pyproject)
        (tmp_path / "sample.py").write_text("x = 1\n")

        proc = subprocess.CompletedProcess(["my-linter-exe"], returncode=0, stdout="")
        mock_run = mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

        returncode = pyfltr.cli.main.run(["run", "--work-dir", str(tmp_path), "--commands=my-linter", str(tmp_path)])
        assert returncode == 0

        invoked_binaries = {
            call.args[0][0] for call in mock_run.call_args_list if call.args and isinstance(call.args[0], list) and call.args[0]
        }
        assert "my-linter-exe" in invoked_binaries


def test_human_readable_disables_structured_output(mocker):
    """--human-readableで構造化出力の引数が注入されない。"""
    proc = subprocess.CompletedProcess(["ruff"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    pyfltr.cli.main.run(["run", "--human-readable", "--commands=ruff-check", str(pathlib.Path(__file__).parents[2])])

    # ruff-checkの実行コマンドラインに--output-format=jsonが含まれないことを確認
    for call in mock_run.call_args_list:
        if call.args and isinstance(call.args[0], list) and "check" in call.args[0]:
            commandline = call.args[0]
            assert "--output-format=json" not in commandline
            break


@pytest.mark.parametrize("fmt", ["jsonl", "sarif", "github-annotations"])
def test_output_format_accepts_structured_choices(mocker, fmt):
    """--output-formatの新choices（jsonl/sarif/github-annotations）が受理される。"""
    proc = subprocess.CompletedProcess(["mypy"], returncode=0, stdout="")
    mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    returncode = pyfltr.cli.main.run(["ci", "--output-format", fmt, "--commands=mypy", str(pathlib.Path(__file__).parents[2])])
    assert returncode == 0


def test_output_format_invalid_choice_rejected():
    """--output-formatの不正値はSystemExit（argparseエラー）。"""
    with pytest.raises(SystemExit):
        pyfltr.cli.main.run(["ci", "--output-format", "bogus", "--commands=mypy", str(pathlib.Path(__file__).parents[2])])


def test_text_output_on_stdout_for_text(mocker, capsys):
    """text formatではstdoutにtext整形出力、stderrにはpyfltrのINFOログは出ない。"""
    proc = subprocess.CompletedProcess(["mypy"], returncode=0, stdout="")
    mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    pyfltr.cli.main.run(["ci", "--output-format=text", "--commands=mypy", str(pathlib.Path(__file__).parents[2])])
    captured = capsys.readouterr()
    assert "summary" in captured.out
    assert "----- pyfltr" in captured.out
    # stderrはsystem logger専用でtext整形は出力しない
    assert "----- summary" not in captured.err


def test_text_output_on_stdout_for_github_annotations(mocker, capsys):
    """github-annotationsはtextと同じレイアウトをstdoutに出力する。"""
    proc = subprocess.CompletedProcess(["mypy"], returncode=0, stdout="")
    mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    pyfltr.cli.main.run(["ci", "--output-format=github-annotations", "--commands=mypy", str(pathlib.Path(__file__).parents[2])])
    captured = capsys.readouterr()
    assert "summary" in captured.out
    assert "----- pyfltr" in captured.out
    assert "----- summary" not in captured.err


def test_jsonl_stdout_keeps_text_on_stderr_with_warn_level(mocker, capsys):
    """jsonl + stdoutモードではtext_loggerがstderrのWARN以上。

    INFOレベルの進捗・summaryはstderrに出ず、stdoutはJSONL専有となる。
    """
    proc = subprocess.CompletedProcess(["mypy"], returncode=0, stdout="")
    mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    pyfltr.cli.main.run(["ci", "--output-format=jsonl", "--commands=mypy", str(pathlib.Path(__file__).parents[2])])
    captured = capsys.readouterr()
    # stdoutはJSONLのみ（textの区切り線は出ない）
    assert "----- pyfltr" not in captured.out
    # INFO進捗・summaryはWARNレベルで抑止されるためstderrにも出ない
    assert "----- summary" not in captured.err
    assert "----- pyfltr" not in captured.err


def test_sarif_stdout_keeps_text_on_stderr_with_info_level(mocker, capsys):
    """sarif + stdoutモードではtext_loggerがstderrのINFOで出力される。"""
    proc = subprocess.CompletedProcess(["mypy"], returncode=0, stdout="")
    mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    pyfltr.cli.main.run(["ci", "--output-format=sarif", "--commands=mypy", str(pathlib.Path(__file__).parents[2])])
    captured = capsys.readouterr()
    # stdoutはSARIF JSON（`"version": "2.1.0"`を含む）でtext整形は混入しない
    assert "----- pyfltr" not in captured.out
    assert '"version": "2.1.0"' in captured.out
    # stderrにINFOレベルのtext整形が出力される
    assert "----- pyfltr" in captured.err
    assert "----- summary" in captured.err


def test_system_logger_always_on_stderr_and_not_suppressed(mocker, capsys):
    """どのformatでもroot loggerは抑止されず、handlersが空にならない。"""
    proc = subprocess.CompletedProcess(["mypy"], returncode=0, stdout="")
    mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    for fmt in ("text", "jsonl", "sarif", "github-annotations"):
        pyfltr.cli.main.run(["ci", "--output-format", fmt, "--commands=mypy", str(pathlib.Path(__file__).parents[2])])
        assert logging.getLogger().handlers, f"root loggerのhandlerが空になっている: fmt={fmt}"
        capsys.readouterr()  # 各回のstdout/stderrを破棄する


@pytest.mark.parametrize("fmt", ["jsonl", "sarif", "github-annotations"])
def test_output_file_keeps_text_on_stdout_for_all_formats(mocker, capsys, tmp_path, fmt):
    """--output-file指定時はstdoutにtext整形出力が出る（どのformatでも）。"""
    proc = subprocess.CompletedProcess(["mypy"], returncode=0, stdout="")
    mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    # github-annotationsは--output-fileを解釈しないためtextモードと同等の挙動となる。
    destination = tmp_path / "out.dat"
    pyfltr.cli.main.run(
        [
            "ci",
            "--output-format",
            fmt,
            f"--output-file={destination}",
            "--commands=mypy",
            str(pathlib.Path(__file__).parents[2]),
        ]
    )
    captured = capsys.readouterr()
    assert "summary" in captured.out
    assert "----- pyfltr" in captured.out


def test_fail_fast_flag_accepted(_isolated_target, mocker):
    """--fail-fastフラグが受理される。"""
    proc = subprocess.CompletedProcess(["test"], returncode=0, stdout="test")
    mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)
    returncode = pyfltr.cli.main.run(["ci", "--work-dir", str(_isolated_target), "--fail-fast", str(_isolated_target)])
    assert returncode == 0


def test_no_cache_flag_accepted(_isolated_target, mocker):
    """--no-cacheフラグが受理される。"""
    proc = subprocess.CompletedProcess(["test"], returncode=0, stdout="test")
    mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)
    returncode = pyfltr.cli.main.run(["ci", "--work-dir", str(_isolated_target), "--no-cache", str(_isolated_target)])
    assert returncode == 0


# --- パートG B案: --only-failed ---


@pytest.fixture
def _only_failed_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> pathlib.Path:
    """--only-failedテスト用にPYFLTR_CACHE_DIRをtmp_pathに固定する。"""
    monkeypatch.setenv("PYFLTR_CACHE_DIR", str(tmp_path))
    return tmp_path


def test_only_failed_flag_accepted(_isolated_target, _only_failed_cache):
    """--only-failedフラグが受理される（直前runが無ければrc=0で成功終了）。

    直前runが存在しないので`run_subprocess`も起動しない経路を通るため
    モック不要。
    """
    returncode = pyfltr.cli.main.run(["ci", "--work-dir", str(_isolated_target), "--only-failed", str(_isolated_target)])
    assert returncode == 0


def test_only_failed_returns_zero_when_no_failures(_isolated_target, mocker, _only_failed_cache):
    """--only-failed指定で直前runに失敗ツールが無ければrc=0で終了する（実コマンド起動無し）。"""
    mocker.patch("pyfltr.command.process.run_subprocess").side_effect = AssertionError("不要な起動")
    returncode = pyfltr.cli.main.run(
        ["run-for-agent", "--work-dir", str(_isolated_target), "--only-failed", str(_isolated_target)]
    )
    assert returncode == 0


# --- --from-runオプション ---


def test_from_run_flag_accepted(_isolated_target, _only_failed_cache):
    """--from-run + --only-failedの併用が受理される（直前runが無ければrc=0で終了）。"""
    returncode = pyfltr.cli.main.run(
        [
            "ci",
            "--work-dir",
            str(_isolated_target),
            "--only-failed",
            "--from-run",
            "latest",
            str(_isolated_target),
        ]
    )
    # アーカイブが空なので「runが存在しない」としてrc=0で終了する
    assert returncode == 0


def test_from_run_without_only_failed_is_error(_isolated_target, _only_failed_cache, capsys):
    """--from-run単独指定（--only-failedなし）はargparseエラー（SystemExit）。"""
    with pytest.raises(SystemExit):
        pyfltr.cli.main.run(["ci", "--work-dir", str(_isolated_target), "--from-run", "latest", str(_isolated_target)])
    captured = capsys.readouterr()
    assert "--from-run" in captured.err


# --- --changed-sinceオプション ---


def test_changed_since_flag_accepted(_isolated_target, mocker):
    """--changed-sinceフラグがargparseに受理される（git差分が空ならrc=0で終了）。"""
    proc = subprocess.CompletedProcess(["test"], returncode=0, stdout="")
    mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)
    # git diff --name-onlyが空を返す（差分なし）ようにスタブする。
    # 差分なし→対象ファイル数0→コマンド起動なし→rc=0で終了する。
    mocker.patch(
        "pyfltr.command.targets._get_changed_files",
        return_value=[],
    )

    returncode = pyfltr.cli.main.run(["ci", "--work-dir", str(_isolated_target), "--changed-since=HEAD", str(_isolated_target)])
    assert returncode == 0


def test_changed_since_with_only_failed(_isolated_target, mocker, _only_failed_cache):
    """--changed-sinceと--only-failedの併用が受理される。

    直前runが存在しないため--only-failedの早期終了経路に到達しrc=0で終了する。
    """
    proc = subprocess.CompletedProcess(["test"], returncode=0, stdout="")
    mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)
    mocker.patch(
        "pyfltr.command.targets._get_changed_files",
        return_value=["some_file.py"],
    )

    returncode = pyfltr.cli.main.run(
        [
            "ci",
            "--work-dir",
            str(_isolated_target),
            "--changed-since=HEAD",
            "--only-failed",
            str(_isolated_target),
        ]
    )
    assert returncode == 0


# --- run_id可視化 ---


@pytest.fixture
def _archive_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> pathlib.Path:
    """archiveテスト用にPYFLTR_CACHE_DIRをtmp_pathに固定する。"""
    monkeypatch.setenv("PYFLTR_CACHE_DIR", str(tmp_path))
    return tmp_path


def test_run_pipeline_logs_run_id_when_archive_enabled(mocker, capsys, _archive_cache):
    """archive有効時、run_pipelineの開始時ログにrun_idとlauncher_prefix整形済みshow-run案内が含まれること。"""
    proc = subprocess.CompletedProcess(["mypy"], returncode=0, stdout="")
    mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)
    # launcher_prefixが環境依存（親プロセス由来）になるため、テスト中は固定値にする。
    mocker.patch("pyfltr.state.retry.detect_launcher_prefix", return_value=["uvx", "pyfltr"])

    pyfltr.cli.main.run(["ci", "--commands=mypy", str(pathlib.Path(__file__).parents[2])])
    captured = capsys.readouterr().out
    assert "run_id:" in captured
    assert "uvx pyfltr show-run" in captured
    assert "で詳細を確認可能" in captured
    # 旧形式（2行分割・latestエイリアス）は出ないこと
    assert "show-run latest" not in captured


def test_run_pipeline_does_not_log_run_id_when_archive_disabled(mocker, capsys, _archive_cache):
    """--no-archive指定時はrun_idログを出力しないこと。"""
    proc = subprocess.CompletedProcess(["mypy"], returncode=0, stdout="")
    mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    pyfltr.cli.main.run(["ci", "--no-archive", "--commands=mypy", str(pathlib.Path(__file__).parents[2])])
    captured = capsys.readouterr().out
    assert "run_id:" not in captured


# --- precommit MM状態ガイダンス ---


def test_precommit_guidance_emitted_when_formatted_under_git(mocker, capsys):
    """formatted結果がありgit commit経由のときガイダンスがstderrに出る。"""
    # ruff-formatがformattedになるようreturncode=1を返す。
    proc_formatted = subprocess.CompletedProcess(["ruff"], returncode=1, stdout="")
    mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc_formatted)
    mocker.patch("pyfltr.command.precommit_guidance.is_invoked_from_git_commit", return_value=True)

    pyfltr.cli.main.run(["run", "--no-ui", "--commands=ruff-format", str(pathlib.Path(__file__).parents[2])])
    captured = capsys.readouterr()
    assert "formatter" in captured.err
    assert "git add" in captured.err


def test_precommit_guidance_skipped_when_not_under_git(mocker, capsys):
    """git commit経由でなければformattedがあってもガイダンスを出力しない。"""
    proc_formatted = subprocess.CompletedProcess(["ruff"], returncode=1, stdout="")
    mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc_formatted)
    mocker.patch("pyfltr.command.precommit_guidance.is_invoked_from_git_commit", return_value=False)

    pyfltr.cli.main.run(["run", "--no-ui", "--commands=ruff-format", str(pathlib.Path(__file__).parents[2])])
    captured = capsys.readouterr()
    assert "formatter" not in captured.err
    assert "git add" not in captured.err


def test_precommit_guidance_skipped_when_no_formatted(mocker, capsys):
    """formatted結果が無ければガイダンスを出力しない。"""
    proc_succeeded = subprocess.CompletedProcess(["ruff"], returncode=0, stdout="")
    mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc_succeeded)
    mocker.patch("pyfltr.command.precommit_guidance.is_invoked_from_git_commit", return_value=True)

    pyfltr.cli.main.run(["run", "--no-ui", "--commands=ruff-format", str(pathlib.Path(__file__).parents[2])])
    captured = capsys.readouterr()
    assert "formatter" not in captured.err
    assert "git add" not in captured.err


def test_tool_name_as_subcommand_shows_guidance(capsys):
    """ツール名をサブコマンドに渡すと実行例付きメッセージをstderrに出力してexit 2。"""
    with pytest.raises(SystemExit) as exc_info:
        pyfltr.cli.main.run(["textlint", "docs/"])
    assert exc_info.value.code == 2
    err = capsys.readouterr().err
    assert "'textlint'" in err
    assert "pyfltr run --commands=textlint docs/" in err
    assert "run-for-agent" not in err


def test_alias_name_as_subcommand_shows_guidance(capsys):
    """`lint`などの静的エイリアスも同じくガイダンスを出力する。"""
    with pytest.raises(SystemExit) as exc_info:
        pyfltr.cli.main.run(["lint", "docs/"])
    assert exc_info.value.code == 2
    err = capsys.readouterr().err
    assert "'lint'" in err
    assert "--commands=lint" in err


def test_argparse_error_prints_help_to_stderr(capsys):
    """argparseエラー時に該当parserの--help相当がstderrに併記されること。"""
    with pytest.raises(SystemExit) as exc_info:
        pyfltr.cli.main.run(["run", "--jobs", "abc"])
    assert exc_info.value.code == 2
    err = capsys.readouterr().err
    # サブパーサー（pyfltr run）のヘルプが出る
    assert "usage:" in err
    assert "--jobs" in err
    # エラー本文も併記される
    assert "invalid int value" in err


def test_invalid_subcommand_prints_main_help(capsys):
    """不正なサブコマンド指定時はメインparserの--help相当が併記される。"""
    with pytest.raises(SystemExit):
        pyfltr.cli.main.run(["invalid-subcommand"])
    err = capsys.readouterr().err
    assert "usage:" in err
    assert "<subcommand>" in err


def test_help_contains_description(capsys):
    """--help出力にdescription（並列実行・エージェント対応）が含まれること。"""
    with pytest.raises(SystemExit) as exc_info:
        pyfltr.cli.main.run(["--help"])
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "並列実行" in out
    assert "コーディングエージェント" in out


def _run_pyfltr_without_mcp(tmp_path: pathlib.Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    """MCPサーバー機能の依存を欠いた環境でpyfltr CLIを起動する。"""
    stub_root = tmp_path / "mcpstub"
    (stub_root / "mcp" / "server").mkdir(parents=True)
    (stub_root / "mcp" / "__init__.py").write_text("")
    (stub_root / "mcp" / "server" / "__init__.py").write_text("")
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join([str(stub_root), env.get("PYTHONPATH", "")])
    return subprocess.run(
        [sys.executable, "-m", "pyfltr", *args],
        cwd=pathlib.Path(__file__).parents[2],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_help_succeeds_without_mcp_server_module(tmp_path: pathlib.Path) -> None:
    """MCPサーバー機能の依存が解決できない環境でも`--help`は成功する。"""
    result = _run_pyfltr_without_mcp(tmp_path, ["--help"])
    assert result.returncode == 0
    # サブコマンド名だけでなく説明文も出ることを確かめ、登録自体が成立していることを示す。
    assert "mcp" in result.stdout
    assert "MCP サーバー" in result.stdout


def test_other_subcommand_succeeds_without_mcp_server_module(tmp_path: pathlib.Path) -> None:
    """MCPサーバー機能の依存が解決できない環境でも他サブコマンドは起動する。"""
    result = _run_pyfltr_without_mcp(tmp_path, ["list-runs", "--help"])
    assert result.returncode == 0
    assert "list-runs" in result.stdout


def test_mcp_subcommand_reports_missing_dependency(tmp_path: pathlib.Path) -> None:
    """MCPサーバー機能の依存が解決できない環境では説明を表示して終了する。"""
    result = _run_pyfltr_without_mcp(tmp_path, ["mcp"])
    assert result.returncode == 1
    assert "必要な依存が解決されていません" in result.stderr


def test_precommit_guidance_skipped_for_jsonl_and_sarif_stdout_only(mocker, capsys):
    """構造化stdoutモード（jsonl/sarif）ではstderrへ漏らさない。

    github-annotationsはtextと同じレイアウトのため`structured_stdout=False`で扱われる。
    """
    proc_formatted = subprocess.CompletedProcess(["ruff"], returncode=1, stdout="")
    mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc_formatted)
    mocker.patch("pyfltr.command.precommit_guidance.is_invoked_from_git_commit", return_value=True)

    # jsonlはstructured_stdoutモード（stdoutをJSONLが占有）のため、ガイダンスをstderrへ出力しない。
    pyfltr.cli.main.run(["run", "--output-format=jsonl", "--commands=ruff-format", str(pathlib.Path(__file__).parents[2])])
    captured = capsys.readouterr()
    assert "formatter" not in captured.err
    assert "git add" not in captured.err


class _FakeReconfigurableStream:
    """`reconfigure`呼び出しを記録するだけのfake stream。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def reconfigure(self, **kwargs: str) -> None:
        self.calls.append(kwargs)


class _ReconfigureRaisingStream:
    """`reconfigure`が例外を上げるfake stream。握り潰し挙動の検証用。"""

    def reconfigure(self, **_kwargs: str) -> None:
        raise OSError("not supported")


def test_reconfigure_stdio_to_utf8_invokes_reconfigure(monkeypatch) -> None:
    """`reconfigure`を持つstreamにはUTF-8 / backslashreplaceが要求される。"""
    fake_stdout = _FakeReconfigurableStream()
    fake_stderr = _FakeReconfigurableStream()
    monkeypatch.setattr(pyfltr.cli.main.sys, "stdout", fake_stdout)
    monkeypatch.setattr(pyfltr.cli.main.sys, "stderr", fake_stderr)

    pyfltr.cli.main._reconfigure_stdio_to_utf8()  # pylint: disable=protected-access

    expected = {"encoding": "utf-8", "errors": "backslashreplace"}
    assert fake_stdout.calls == [expected]
    assert fake_stderr.calls == [expected]


def test_reconfigure_stdio_to_utf8_tolerates_missing_or_failing_streams(monkeypatch) -> None:
    """`reconfigure`未提供streamや呼び出し失敗時に例外が伝播しない。"""
    monkeypatch.setattr(pyfltr.cli.main.sys, "stdout", object())
    monkeypatch.setattr(pyfltr.cli.main.sys, "stderr", _ReconfigureRaisingStream())

    pyfltr.cli.main._reconfigure_stdio_to_utf8()  # pylint: disable=protected-access


# configサブコマンドのテストは、対象サブコマンド単位で見通しを保つため`tests/main_config_test.py`へ分離済み。


# ---------------------------------------------------------------------------
# --quiet オプションのE2Eテスト
# ---------------------------------------------------------------------------


_QUIET_HEADER_FIELDS = {"kind", "commands", "files", "run_id"}


def _parse_jsonl(text: str) -> list[dict]:
    """capsys出力からJSONL行のリストへパースする。"""
    return [json.loads(line) for line in text.splitlines() if line.strip()]


@pytest.mark.parametrize(
    ("argv_tail", "expect_quiet"),
    [([], True), (["--no-quiet"], False)],
)
def test_run_for_agent_quiet_default_and_override(_isolated_target, mocker, capsys, argv_tail, expect_quiet):
    """`run-for-agent`はquiet既定有効、`--no-quiet`で従来のverbose挙動へ戻る。"""
    proc = subprocess.CompletedProcess(["mypy"], returncode=0, stdout="")
    mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)
    argv = ["run-for-agent", "--work-dir", str(_isolated_target), *argv_tail, str(_isolated_target)]
    assert pyfltr.cli.main.run(argv) == 0
    parsed = _parse_jsonl(capsys.readouterr().out)
    header = parsed[0]
    assert header["kind"] == "header"
    if expect_quiet:
        assert set(header.keys()) <= _QUIET_HEADER_FIELDS
        assert not any(r["kind"] == "command" for r in parsed)
    else:
        assert {"version", "uv"} <= header.keys()
        assert any(r["kind"] == "command" for r in parsed)


def test_run_output_format_jsonl_defaults_verbose(_isolated_target, mocker, capsys):
    """エージェント検出変数が無い環境の`pyfltr run --output-format=jsonl`は既定でquiet=Falseとなる。

    `tests/conftest.py`の`_isolate_output_format_envs`が検出変数を未設定化するため、
    本ケースは検出なしの経路を通る。
    """
    proc = subprocess.CompletedProcess(["mypy"], returncode=0, stdout="")
    mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)
    argv = ["run", "--work-dir", str(_isolated_target), "--output-format=jsonl", str(_isolated_target)]
    assert pyfltr.cli.main.run(argv) == 0
    header = _parse_jsonl(capsys.readouterr().out)[0]
    assert {"version", "uv"} <= header.keys()


def test_run_for_agent_quiet_applies_to_early_run_ctx(tmp_path, mocker, capsys):
    """全ターゲット不在時のearly_run_ctx経路でも`--quiet`が適用され、headerが縮約される。"""
    (tmp_path / "pyproject.toml").write_text("[tool.pyfltr]\n")
    mocker.patch("pyfltr.command.process.run_subprocess", return_value=subprocess.CompletedProcess(["x"], 0, ""))
    original_cwd = pathlib.Path.cwd()
    try:
        os.chdir(tmp_path)
        returncode = pyfltr.cli.main.run(["run-for-agent", "does_not_exist.py"])
    finally:
        os.chdir(original_cwd)
    assert returncode == 1
    header = next(r for r in _parse_jsonl(capsys.readouterr().out) if r["kind"] == "header")
    assert set(header.keys()) <= _QUIET_HEADER_FIELDS
