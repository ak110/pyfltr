"""mainエントリーポイントのテストコード。"""

import json
import logging
import os
import pathlib
import subprocess

import pytest

import pyfltr.cli.main
import pyfltr.cli.precommit_guidance


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


def test_precommit_guidance_emitted_when_formatted_under_git(mocker, capsys):
    """formatted結果がありgit commit経由のときガイダンスがstderrに出る。"""
    # ruff-formatがformatted（returncode=1, has_error=False）になるよう制御する。
    proc_formatted = subprocess.CompletedProcess(["ruff"], returncode=1, stdout="")
    mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc_formatted)
    mocker.patch("pyfltr.cli.precommit_guidance.is_invoked_from_git_commit", return_value=True)

    pyfltr.cli.main.run(["run", "--no-ui", "--commands=ruff-format", str(pathlib.Path(__file__).parent.parent)])
    captured = capsys.readouterr()
    assert "formatter" in captured.err
    assert "git add" in captured.err


def test_precommit_guidance_skipped_when_not_under_git(mocker, capsys):
    """git commit経由でなければformattedがあってもガイダンスを出力しない。"""
    proc_formatted = subprocess.CompletedProcess(["ruff"], returncode=1, stdout="")
    mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc_formatted)
    mocker.patch("pyfltr.cli.precommit_guidance.is_invoked_from_git_commit", return_value=False)

    pyfltr.cli.main.run(["run", "--no-ui", "--commands=ruff-format", str(pathlib.Path(__file__).parent.parent)])
    captured = capsys.readouterr()
    assert "formatter" not in captured.err
    assert "git add" not in captured.err


def test_precommit_guidance_skipped_when_no_formatted(mocker, capsys):
    """formatted結果が無ければガイダンスを出力しない。"""
    proc_succeeded = subprocess.CompletedProcess(["ruff"], returncode=0, stdout="")
    mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc_succeeded)
    mocker.patch("pyfltr.cli.precommit_guidance.is_invoked_from_git_commit", return_value=True)

    pyfltr.cli.main.run(["run", "--no-ui", "--commands=ruff-format", str(pathlib.Path(__file__).parent.parent)])
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


def test_help_contains_description(capsys):
    """--help出力にdescription（並列実行・エージェント対応）が含まれること。"""
    with pytest.raises(SystemExit) as exc_info:
        pyfltr.cli.main.run(["--help"])
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "並列実行" in out
    assert "コーディングエージェント" in out


def test_precommit_guidance_skipped_for_jsonl_and_sarif_stdout_only(mocker, capsys):
    """構造化stdoutモード（jsonl/sarif）ではstderrへ漏らさない。

    github-annotationsはtextと同じレイアウトのため`structured_stdout=False`で扱われる。
    """
    proc_formatted = subprocess.CompletedProcess(["ruff"], returncode=1, stdout="")
    mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc_formatted)
    mocker.patch("pyfltr.cli.precommit_guidance.is_invoked_from_git_commit", return_value=True)

    # jsonlはstructured_stdoutモード（stdoutをJSONLが占有）のため、ガイダンスをstderrへ出力しない。
    pyfltr.cli.main.run(["run", "--output-format=jsonl", "--commands=ruff-format", str(pathlib.Path(__file__).parent.parent)])
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


# configサブコマンドのテストは`tests/main_config_test.py`へ分離済み（pylintのtoo-many-lines対策）。


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
    """`pyfltr run --output-format=jsonl`は既定でquiet=Falseとなる（従来挙動維持）。"""
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
