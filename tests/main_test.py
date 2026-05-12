"""mainエントリーポイントのテストコード。"""

# pylint: disable=protected-access

import json
import logging
import os
import pathlib
import subprocess

import pytest

import pyfltr.cli.main
import pyfltr.cli.pipeline
import pyfltr.cli.precommit_guidance
import pyfltr.command.process
import pyfltr.warnings_
from tests import conftest as _testconf


@pytest.mark.parametrize("mode", ["run", "ci"])
def test_success(mocker, mode):
    proc = subprocess.CompletedProcess(["test"], returncode=0, stdout="test")
    mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)
    returncode = pyfltr.cli.main.run([mode, str(pathlib.Path(__file__).parent.parent)])
    assert returncode == 0


@pytest.mark.parametrize("mode", ["run", "ci"])
def test_fail(mocker, mode):
    proc = subprocess.CompletedProcess(["test"], returncode=-1, stdout="test")
    mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)
    returncode = pyfltr.cli.main.run([mode, str(pathlib.Path(__file__).parent.parent)])
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
        ["ci", "--work-dir", str(tmp_path), "--commands=pytest", str(pathlib.Path(__file__).parent.parent)]
    )
    assert returncode == 0
    # cwdが復元されていることを確認
    assert pathlib.Path.cwd() == original_cwd


def test_run_auto_includes_fix_stage(mocker):
    """runサブコマンドではfix-args付きのfixステージが自動実行される。"""
    proc = subprocess.CompletedProcess(["test"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    # ruff-checkはfix-args定義済みかつpreset=latestで有効化されている
    returncode = pyfltr.cli.main.run(["run", "--commands=ruff-check", str(pathlib.Path(__file__).parent.parent)])
    assert returncode == 0

    # 通常モードの引数リストとfixモードの引数リストが両方実行されていること
    invoked_commandlines = [call.args[0] for call in mock_run.call_args_list if call.args and isinstance(call.args[0], list)]
    fix_calls = [cl for cl in invoked_commandlines if "--fix" in cl]
    assert fix_calls, "fixステージが実行されていない"


def test_no_fix_skips_fix_stage(mocker):
    """--no-fix指定時はfixステージがスキップされる。"""
    proc = subprocess.CompletedProcess(["test"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    returncode = pyfltr.cli.main.run(["run", "--no-fix", "--commands=ruff-check", str(pathlib.Path(__file__).parent.parent)])
    assert returncode == 0

    invoked_commandlines = [call.args[0] for call in mock_run.call_args_list if call.args and isinstance(call.args[0], list)]
    fix_calls = [cl for cl in invoked_commandlines if "--fix" in cl]
    assert not fix_calls, "--no-fix指定時にfixステージが実行されている"


def test_ci_does_not_run_fix_stage(mocker):
    """ciサブコマンドではfixステージを実行しない（ファイル書換を避けるため）。"""
    proc = subprocess.CompletedProcess(["test"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    pyfltr.cli.main.run(["ci", "--commands=ruff-check", str(pathlib.Path(__file__).parent.parent)])

    invoked_commandlines = [call.args[0] for call in mock_run.call_args_list if call.args and isinstance(call.args[0], list)]
    fix_calls = [cl for cl in invoked_commandlines if "--fix" in cl]
    assert not fix_calls, "ciサブコマンドでfixステージが実行されている"


def test_stream_mode_writes_detail_log_during_run(mocker, capsys):
    """--stream指定時はコマンド完了時に詳細ログが出力される。"""
    # pyfltrルートのpyproject.tomlにはpython=trueが設定されているためmypyは有効
    proc = subprocess.CompletedProcess(["mypy"], returncode=0, stdout="mypy-detail")
    mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    returncode = pyfltr.cli.main.run(
        ["ci", "--no-ui", "--stream", "--commands=mypy", str(pathlib.Path(__file__).parent.parent)]
    )
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

    returncode = pyfltr.cli.main.run(["ci", "--no-ui", "--commands=mypy", str(pathlib.Path(__file__).parent.parent)])
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
        ["ci", "--commands=pytest", "--pytest-args=--maxfail=5 -v", str(pathlib.Path(__file__).parent.parent)]
    )
    assert returncode == 0

    # subprocess.runが呼ばれた引数を確認
    assert mock_run.called
    called_args = mock_run.call_args[0][0]  # 最初の引数（コマンドライン）
    assert "--maxfail=5" in called_args
    assert "-v" in called_args


class TestSubcommandIntegration:
    """サブコマンドの統合テスト。"""

    def test_run_subcommand(self, mocker):
        """runサブコマンドで--exit-zero-even-if-formattedが暗黙的に有効化される。"""
        proc = subprocess.CompletedProcess(["test"], returncode=0, stdout="test")
        mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)
        returncode = pyfltr.cli.main.run(["run", str(pathlib.Path(__file__).parent.parent)])
        assert returncode == 0

    def test_fast_subcommand(self, mocker):
        """fastサブコマンドで--exit-zero-even-if-formattedと--commands=fastが暗黙的に有効化される。"""
        proc = subprocess.CompletedProcess(["test"], returncode=0, stdout="test")
        mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)
        returncode = pyfltr.cli.main.run(["fast", str(pathlib.Path(__file__).parent.parent)])
        assert returncode == 0

    def test_run_for_agent_subcommand(self, mocker):
        """run-for-agentサブコマンドで--exit-zero-even-if-formattedと--output-format=jsonlが暗黙的に有効化される。"""
        proc = subprocess.CompletedProcess(["test"], returncode=0, stdout="test")
        mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)
        returncode = pyfltr.cli.main.run(["run-for-agent", str(pathlib.Path(__file__).parent.parent)])
        assert returncode == 0

    def test_ci_explicit(self, mocker):
        """明示的なciサブコマンド。"""
        proc = subprocess.CompletedProcess(["test"], returncode=0, stdout="test")
        mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)
        returncode = pyfltr.cli.main.run(["ci", str(pathlib.Path(__file__).parent.parent)])
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

    pyfltr.cli.main.run(["run", "--human-readable", "--commands=ruff-check", str(pathlib.Path(__file__).parent.parent)])

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

    returncode = pyfltr.cli.main.run(
        ["ci", "--output-format", fmt, "--commands=mypy", str(pathlib.Path(__file__).parent.parent)]
    )
    assert returncode == 0


def test_output_format_invalid_choice_rejected():
    """--output-formatの不正値はSystemExit（argparseエラー）。"""
    with pytest.raises(SystemExit):
        pyfltr.cli.main.run(["ci", "--output-format", "bogus", "--commands=mypy", str(pathlib.Path(__file__).parent.parent)])


def test_text_output_on_stdout_for_text(mocker, capsys):
    """text formatではstdoutにtext整形出力、stderrにはpyfltrのINFOログは出ない。"""
    proc = subprocess.CompletedProcess(["mypy"], returncode=0, stdout="")
    mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    pyfltr.cli.main.run(["ci", "--output-format=text", "--commands=mypy", str(pathlib.Path(__file__).parent.parent)])
    captured = capsys.readouterr()
    assert "summary" in captured.out
    assert "----- pyfltr" in captured.out
    # stderrはsystem logger専用でtext整形は出力しない
    assert "----- summary" not in captured.err


def test_text_output_on_stdout_for_github_annotations(mocker, capsys):
    """github-annotationsはtextと同じレイアウトをstdoutに出力する。"""
    proc = subprocess.CompletedProcess(["mypy"], returncode=0, stdout="")
    mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    pyfltr.cli.main.run(
        ["ci", "--output-format=github-annotations", "--commands=mypy", str(pathlib.Path(__file__).parent.parent)]
    )
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

    pyfltr.cli.main.run(["ci", "--output-format=jsonl", "--commands=mypy", str(pathlib.Path(__file__).parent.parent)])
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

    pyfltr.cli.main.run(["ci", "--output-format=sarif", "--commands=mypy", str(pathlib.Path(__file__).parent.parent)])
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
        pyfltr.cli.main.run(["ci", "--output-format", fmt, "--commands=mypy", str(pathlib.Path(__file__).parent.parent)])
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
            str(pathlib.Path(__file__).parent.parent),
        ]
    )
    captured = capsys.readouterr()
    assert "summary" in captured.out
    assert "----- pyfltr" in captured.out


def test_fail_fast_flag_accepted(mocker):
    """--fail-fastフラグが受理される。"""
    proc = subprocess.CompletedProcess(["test"], returncode=0, stdout="test")
    mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)
    returncode = pyfltr.cli.main.run(["ci", "--fail-fast", str(pathlib.Path(__file__).parent.parent)])
    assert returncode == 0


def test_no_cache_flag_accepted(mocker):
    """--no-cacheフラグが受理される。"""
    proc = subprocess.CompletedProcess(["test"], returncode=0, stdout="test")
    mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)
    returncode = pyfltr.cli.main.run(["ci", "--no-cache", str(pathlib.Path(__file__).parent.parent)])
    assert returncode == 0


# --- パートG B案: --only-failed ---


@pytest.fixture
def _only_failed_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> pathlib.Path:
    """--only-failedテスト用にPYFLTR_CACHE_DIRをtmp_pathに固定する。"""
    monkeypatch.setenv("PYFLTR_CACHE_DIR", str(tmp_path))
    return tmp_path


def test_only_failed_flag_accepted(_only_failed_cache):
    """--only-failedフラグが受理される（直前runが無ければrc=0で成功終了）。

    直前runが存在しないので`run_subprocess`も起動しない経路を通るため
    モック不要。
    """
    returncode = pyfltr.cli.main.run(["ci", "--only-failed", str(pathlib.Path(__file__).parent.parent)])
    assert returncode == 0


def test_only_failed_returns_zero_when_no_failures(mocker, _only_failed_cache):
    """--only-failed指定で直前runに失敗ツールが無ければrc=0で終了する（実コマンド起動無し）。"""
    mocker.patch("pyfltr.command.process.run_subprocess").side_effect = AssertionError("不要な起動")
    returncode = pyfltr.cli.main.run(["run-for-agent", "--only-failed", str(pathlib.Path(__file__).parent.parent)])
    assert returncode == 0


# --- --from-runオプション ---


def test_from_run_flag_accepted(_only_failed_cache):
    """--from-run + --only-failedの併用が受理される（直前runが無ければrc=0で終了）。"""
    returncode = pyfltr.cli.main.run(["ci", "--only-failed", "--from-run", "latest", str(pathlib.Path(__file__).parent.parent)])
    # アーカイブが空なので「runが存在しない」としてrc=0で終了する
    assert returncode == 0


def test_from_run_without_only_failed_is_error(_only_failed_cache, capsys):
    """--from-run単独指定（--only-failedなし）はargparseエラー（SystemExit）。"""
    with pytest.raises(SystemExit):
        pyfltr.cli.main.run(["ci", "--from-run", "latest", str(pathlib.Path(__file__).parent.parent)])
    captured = capsys.readouterr()
    assert "--from-run" in captured.err


# --- --changed-sinceオプション ---


def test_changed_since_flag_accepted(mocker):
    """--changed-sinceフラグがargparseに受理される（git差分が空ならrc=0で終了）。"""
    proc = subprocess.CompletedProcess(["test"], returncode=0, stdout="")
    mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)
    # git diff --name-onlyが空を返す（差分なし）ようにスタブする。
    # 差分なし→対象ファイル数0→コマンド起動なし→rc=0で終了する。
    mocker.patch(
        "pyfltr.command.targets._get_changed_files",
        return_value=[],
    )

    returncode = pyfltr.cli.main.run(["ci", "--changed-since=HEAD", str(pathlib.Path(__file__).parent.parent)])
    assert returncode == 0


def test_changed_since_with_only_failed(mocker, _only_failed_cache):
    """--changed-sinceと--only-failedの併用が受理される。

    直前runが存在しないため--only-failedの早期終了経路に到達しrc=0で終了する。
    """
    proc = subprocess.CompletedProcess(["test"], returncode=0, stdout="")
    mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)
    mocker.patch(
        "pyfltr.command.targets._get_changed_files",
        return_value=["some_file.py"],
    )

    returncode = pyfltr.cli.main.run(["ci", "--changed-since=HEAD", "--only-failed", str(pathlib.Path(__file__).parent.parent)])
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

    pyfltr.cli.main.run(["ci", "--commands=mypy", str(pathlib.Path(__file__).parent.parent)])
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

    pyfltr.cli.main.run(["ci", "--no-archive", "--commands=mypy", str(pathlib.Path(__file__).parent.parent)])
    captured = capsys.readouterr().out
    assert "run_id:" not in captured


# --- precommit MM状態ガイダンス ---


def test_precommit_guidance_emitted_when_formatted_under_git(monkeypatch, capsys):
    """formatted結果がありgit commit経由のときガイダンスがstderrに出る。"""
    monkeypatch.setattr(pyfltr.cli.precommit_guidance, "is_invoked_from_git_commit", lambda: True)
    pyfltr.cli.pipeline._maybe_emit_precommit_guidance(
        [_testconf.make_formatted_result(), _testconf.make_succeeded_result()],
        structured_stdout=False,
    )
    captured = capsys.readouterr()
    assert "formatter" in captured.err
    assert "git add" in captured.err


def test_precommit_guidance_skipped_when_not_under_git(monkeypatch, capsys):
    """git commit経由でなければformattedがあってもガイダンスを出力しない。"""
    monkeypatch.setattr(pyfltr.cli.precommit_guidance, "is_invoked_from_git_commit", lambda: False)
    pyfltr.cli.pipeline._maybe_emit_precommit_guidance(
        [_testconf.make_formatted_result()],
        structured_stdout=False,
    )
    captured = capsys.readouterr()
    assert captured.err == ""


def test_precommit_guidance_skipped_when_no_formatted(monkeypatch, capsys):
    """formatted結果が無ければガイダンスを出力しない。"""
    monkeypatch.setattr(pyfltr.cli.precommit_guidance, "is_invoked_from_git_commit", lambda: True)
    pyfltr.cli.pipeline._maybe_emit_precommit_guidance(
        [_testconf.make_succeeded_result()],
        structured_stdout=False,
    )
    captured = capsys.readouterr()
    assert captured.err == ""


def test_tool_name_as_subcommand_shows_guidance(capsys):
    """ツール名をサブコマンドに渡すと実行例付きメッセージをstderrに出力してexit 2。"""
    with pytest.raises(SystemExit) as exc_info:
        pyfltr.cli.main.run(["textlint", "docs/"])
    assert exc_info.value.code == 2
    err = capsys.readouterr().err
    assert "'textlint'" in err
    assert "pyfltr run --commands=textlint docs/" in err
    assert "pyfltr run-for-agent --commands=textlint docs/" in err


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


def test_precommit_guidance_skipped_for_jsonl_and_sarif_stdout_only(monkeypatch, capsys):
    """構造化stdoutモード（jsonl/sarif）ではstderrへ漏らさない。

    github-annotationsはtextと同じレイアウトのため`structured_stdout=False`で扱われる。
    """
    monkeypatch.setattr(pyfltr.cli.precommit_guidance, "is_invoked_from_git_commit", lambda: True)
    pyfltr.cli.pipeline._maybe_emit_precommit_guidance(
        [_testconf.make_formatted_result()],
        structured_stdout=True,
    )
    captured = capsys.readouterr()
    assert captured.err == ""


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

    pyfltr.cli.main._reconfigure_stdio_to_utf8()

    expected = {"encoding": "utf-8", "errors": "backslashreplace"}
    assert fake_stdout.calls == [expected]
    assert fake_stderr.calls == [expected]


def test_reconfigure_stdio_to_utf8_tolerates_missing_or_failing_streams(monkeypatch) -> None:
    """`reconfigure`未提供streamや呼び出し失敗時に例外が伝播しない。"""
    monkeypatch.setattr(pyfltr.cli.main.sys, "stdout", object())
    monkeypatch.setattr(pyfltr.cli.main.sys, "stderr", _ReconfigureRaisingStream())

    pyfltr.cli.main._reconfigure_stdio_to_utf8()


# --- configサブコマンド ---


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

    def test_config_list_ai_agent_jsonl(self, monkeypatch, tmp_path, capsys) -> None:
        """AI_AGENT 設定時、`config list` は --output-format 未指定でも JSONL を出力する。"""
        (tmp_path / "pyproject.toml").write_text("[tool.pyfltr]\narchive-max-age-days = 5\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("AI_AGENT", "1")
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
