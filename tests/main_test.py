# pylint: disable=missing-module-docstring
# pylint: disable=missing-function-docstring
# pylint: disable=protected-access

import logging
import pathlib
import subprocess

import pytest

import pyfltr.command
import pyfltr.main


@pytest.mark.parametrize("mode", ["run", "ci"])
def test_success(mocker, mode):
    proc = subprocess.CompletedProcess(["test"], returncode=0, stdout="test")
    mocker.patch("pyfltr.command._run_subprocess", return_value=proc)
    returncode = pyfltr.main.run([mode, str(pathlib.Path(__file__).parent.parent)])
    assert returncode == 0


@pytest.mark.parametrize("mode", ["run", "ci"])
def test_fail(mocker, mode):
    proc = subprocess.CompletedProcess(["test"], returncode=-1, stdout="test")
    mocker.patch("pyfltr.command._run_subprocess", return_value=proc)
    returncode = pyfltr.main.run([mode, str(pathlib.Path(__file__).parent.parent)])
    assert returncode == 1


def test_missing_subcommand_errors():
    """サブコマンド未指定時に SystemExit が発生することを確認。"""
    with pytest.raises(SystemExit):
        pyfltr.main.run([])


def test_work_dir(mocker, tmp_path):
    """--work-dirオプションのテスト。"""
    (tmp_path / "pyproject.toml").write_text('[tool.pyfltr]\npreset = "latest"\n')
    proc = subprocess.CompletedProcess(["test"], returncode=0, stdout="test")
    mocker.patch("pyfltr.command._run_subprocess", return_value=proc)
    original_cwd = pathlib.Path.cwd()
    returncode = pyfltr.main.run(
        ["ci", "--work-dir", str(tmp_path), "--commands=pytest", str(pathlib.Path(__file__).parent.parent)]
    )
    assert returncode == 0
    # cwdが復元されていることを確認
    assert pathlib.Path.cwd() == original_cwd


def test_run_auto_includes_fix_stage(mocker):
    """run サブコマンドでは fix-args 付きの fix ステージが自動実行される。"""
    proc = subprocess.CompletedProcess(["test"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command._run_subprocess", return_value=proc)

    # ruff-check は fix-args 定義済みかつ preset=latest で有効化されている
    returncode = pyfltr.main.run(["run", "--commands=ruff-check", str(pathlib.Path(__file__).parent.parent)])
    assert returncode == 0

    # 通常モードの引数リストと fix モードの引数リストが両方実行されていること
    invoked_commandlines = [call.args[0] for call in mock_run.call_args_list if call.args and isinstance(call.args[0], list)]
    fix_calls = [cl for cl in invoked_commandlines if "--fix" in cl]
    assert fix_calls, "fix ステージが実行されていない"


def test_no_fix_skips_fix_stage(mocker):
    """--no-fix 指定時は fix ステージがスキップされる。"""
    proc = subprocess.CompletedProcess(["test"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command._run_subprocess", return_value=proc)

    returncode = pyfltr.main.run(["run", "--no-fix", "--commands=ruff-check", str(pathlib.Path(__file__).parent.parent)])
    assert returncode == 0

    invoked_commandlines = [call.args[0] for call in mock_run.call_args_list if call.args and isinstance(call.args[0], list)]
    fix_calls = [cl for cl in invoked_commandlines if "--fix" in cl]
    assert not fix_calls, "--no-fix 指定時に fix ステージが走っている"


def test_ci_does_not_run_fix_stage(mocker):
    """ci サブコマンドでは fix ステージを走らせない（ファイル書換を避けるため）。"""
    proc = subprocess.CompletedProcess(["test"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command._run_subprocess", return_value=proc)

    pyfltr.main.run(["ci", "--commands=ruff-check", str(pathlib.Path(__file__).parent.parent)])

    invoked_commandlines = [call.args[0] for call in mock_run.call_args_list if call.args and isinstance(call.args[0], list)]
    fix_calls = [cl for cl in invoked_commandlines if "--fix" in cl]
    assert not fix_calls, "ci サブコマンドで fix ステージが走っている"


def test_stream_mode_writes_detail_log_during_run(mocker, caplog):
    """--stream 指定時はコマンド完了時に詳細ログが出力される。"""
    # pyfltr ルートの pyproject.toml には python=true が設定されているため mypy は有効
    proc = subprocess.CompletedProcess(["mypy"], returncode=0, stdout="mypy-detail")
    mocker.patch("pyfltr.command._run_subprocess", return_value=proc)

    returncode = pyfltr.main.run(["ci", "--no-ui", "--stream", "--commands=mypy", str(pathlib.Path(__file__).parent.parent)])
    assert returncode == 0
    # 詳細ログに含まれる returncode 行が出力される
    assert "returncode: 0" in caplog.text
    # summary セクションも引き続き出力される
    assert "summary" in caplog.text


def test_buffered_mode_is_default(mocker, caplog):
    """既定では 成功コマンド詳細 → summary の順でまとめて出力される。"""
    # pyfltr ルートの pyproject.toml には python=true が設定されているため mypy は有効
    proc = subprocess.CompletedProcess(["mypy"], returncode=0, stdout="mypy-detail")
    mocker.patch("pyfltr.command._run_subprocess", return_value=proc)

    returncode = pyfltr.main.run(["ci", "--no-ui", "--commands=mypy", str(pathlib.Path(__file__).parent.parent)])
    assert returncode == 0
    text = caplog.text
    # 詳細ログが summary より先に来る (summary は末尾)
    assert "summary" in text
    assert "returncode: 0" in text
    assert text.index("returncode: 0") < text.index("summary")


def test_additional_args(mocker):
    """追加引数のテスト。"""
    proc = subprocess.CompletedProcess(["pytest"], returncode=0, stdout="test")
    mock_run = mocker.patch("pyfltr.command._run_subprocess", return_value=proc)

    returncode = pyfltr.main.run(
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
        mocker.patch("pyfltr.command._run_subprocess", return_value=proc)
        returncode = pyfltr.main.run(["run", str(pathlib.Path(__file__).parent.parent)])
        assert returncode == 0

    def test_fast_subcommand(self, mocker):
        """fastサブコマンドで--exit-zero-even-if-formattedと--commands=fastが暗黙的に有効化される。"""
        proc = subprocess.CompletedProcess(["test"], returncode=0, stdout="test")
        mocker.patch("pyfltr.command._run_subprocess", return_value=proc)
        returncode = pyfltr.main.run(["fast", str(pathlib.Path(__file__).parent.parent)])
        assert returncode == 0

    def test_run_for_agent_subcommand(self, mocker):
        """run-for-agentサブコマンドで--exit-zero-even-if-formattedと--output-format=jsonlが暗黙的に有効化される。"""
        proc = subprocess.CompletedProcess(["test"], returncode=0, stdout="test")
        mocker.patch("pyfltr.command._run_subprocess", return_value=proc)
        returncode = pyfltr.main.run(["run-for-agent", str(pathlib.Path(__file__).parent.parent)])
        assert returncode == 0

    def test_ci_explicit(self, mocker):
        """明示的なciサブコマンド。"""
        proc = subprocess.CompletedProcess(["test"], returncode=0, stdout="test")
        mocker.patch("pyfltr.command._run_subprocess", return_value=proc)
        returncode = pyfltr.main.run(["ci", str(pathlib.Path(__file__).parent.parent)])
        assert returncode == 0

    def test_run_includes_custom_commands_by_default(self, mocker, tmp_path):
        """`run` サブコマンドで custom-commands を --commands で明示すると実行される。"""
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
        mock_run = mocker.patch("pyfltr.command._run_subprocess", return_value=proc)

        returncode = pyfltr.main.run(["run", "--work-dir", str(tmp_path), "--commands=my-linter", str(tmp_path)])
        assert returncode == 0

        invoked_binaries = {
            call.args[0][0] for call in mock_run.call_args_list if call.args and isinstance(call.args[0], list) and call.args[0]
        }
        assert "my-linter-exe" in invoked_binaries


def test_human_readable_disables_structured_output(mocker):
    """--human-readable で構造化出力の引数が注入されない。"""
    proc = subprocess.CompletedProcess(["ruff"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command._run_subprocess", return_value=proc)

    pyfltr.main.run(["run", "--human-readable", "--commands=ruff-check", str(pathlib.Path(__file__).parent.parent)])

    # ruff-check の実行コマンドラインに --output-format=json が含まれないことを確認
    for call in mock_run.call_args_list:
        if call.args and isinstance(call.args[0], list) and "check" in call.args[0]:
            commandline = call.args[0]
            assert "--output-format=json" not in commandline
            break


@pytest.mark.parametrize("fmt", ["jsonl", "sarif", "github-annotations"])
def test_output_format_accepts_structured_choices(mocker, fmt):
    """--output-format の新 choices (jsonl/sarif/github-annotations) が受理される。"""
    proc = subprocess.CompletedProcess(["mypy"], returncode=0, stdout="")
    mocker.patch("pyfltr.command._run_subprocess", return_value=proc)

    returncode = pyfltr.main.run(["ci", "--output-format", fmt, "--commands=mypy", str(pathlib.Path(__file__).parent.parent)])
    assert returncode == 0


def test_output_format_invalid_choice_rejected():
    """--output-format の不正値は SystemExit (argparse エラー)。"""
    with pytest.raises(SystemExit):
        pyfltr.main.run(["ci", "--output-format", "bogus", "--commands=mypy", str(pathlib.Path(__file__).parent.parent)])


@pytest.mark.parametrize("fmt", ["sarif", "github-annotations"])
def test_structured_stdout_suppresses_logging(mocker, capsys, fmt):
    """SARIF / GitHub Annotation 出力時も stdout が構造化出力に専有される。"""
    proc = subprocess.CompletedProcess(["mypy"], returncode=0, stdout="")
    mocker.patch("pyfltr.command._run_subprocess", return_value=proc)

    pyfltr.main.run(["ci", "--output-format", fmt, "--commands=mypy", str(pathlib.Path(__file__).parent.parent)])
    captured = capsys.readouterr()
    # stderr には pyfltr のログが漏れない
    assert "pyfltr" not in captured.err


def test_fail_fast_flag_accepted(mocker):
    """--fail-fast フラグが受理される。"""
    proc = subprocess.CompletedProcess(["test"], returncode=0, stdout="test")
    mocker.patch("pyfltr.command._run_subprocess", return_value=proc)
    returncode = pyfltr.main.run(["ci", "--fail-fast", str(pathlib.Path(__file__).parent.parent)])
    assert returncode == 0


def test_no_cache_flag_accepted(mocker):
    """--no-cache フラグが受理される。"""
    proc = subprocess.CompletedProcess(["test"], returncode=0, stdout="test")
    mocker.patch("pyfltr.command._run_subprocess", return_value=proc)
    returncode = pyfltr.main.run(["ci", "--no-cache", str(pathlib.Path(__file__).parent.parent)])
    assert returncode == 0


# --- パートG B案: --only-failed ---


@pytest.fixture
def _only_failed_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> pathlib.Path:
    """--only-failed テスト用に PYFLTR_CACHE_DIR を tmp_path に固定する。"""
    monkeypatch.setenv("PYFLTR_CACHE_DIR", str(tmp_path))
    return tmp_path


def test_only_failed_flag_accepted(_only_failed_cache):
    """--only-failed フラグが受理される (直前 run が無ければ rc=0 で成功終了)。

    直前 run が存在しないので ``_run_subprocess`` も起動しない経路を通るため
    モック不要。
    """
    returncode = pyfltr.main.run(["ci", "--only-failed", str(pathlib.Path(__file__).parent.parent)])
    assert returncode == 0


def test_only_failed_returns_zero_when_no_failures(mocker, _only_failed_cache):
    """--only-failed 指定で直前 run に失敗ツールが無ければ rc=0 で終了する (実コマンド起動無し)。"""
    mocker.patch("pyfltr.command._run_subprocess").side_effect = AssertionError("不要な起動")
    returncode = pyfltr.main.run(["run-for-agent", "--only-failed", str(pathlib.Path(__file__).parent.parent)])
    assert returncode == 0


# --- --from-run オプション ---


def test_from_run_flag_accepted(_only_failed_cache):
    """--from-run + --only-failed の併用が受理される（直前 run が無ければ rc=0 で終了）。"""
    returncode = pyfltr.main.run(["ci", "--only-failed", "--from-run", "latest", str(pathlib.Path(__file__).parent.parent)])
    # アーカイブが空なので「run が存在しない」として rc=0 で終了する
    assert returncode == 0


def test_from_run_without_only_failed_is_error(_only_failed_cache, capsys):
    """--from-run 単独指定（--only-failed なし）は argparse エラー (SystemExit)。"""
    with pytest.raises(SystemExit):
        pyfltr.main.run(["ci", "--from-run", "latest", str(pathlib.Path(__file__).parent.parent)])
    captured = capsys.readouterr()
    assert "--from-run" in captured.err


# --- run_id 可視化 ---


@pytest.fixture
def _archive_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> pathlib.Path:
    """archive テスト用に PYFLTR_CACHE_DIR を tmp_path に固定する。"""
    monkeypatch.setenv("PYFLTR_CACHE_DIR", str(tmp_path))
    return tmp_path


def test_run_pipeline_logs_run_id_when_archive_enabled(mocker, caplog, _archive_cache):
    """archive 有効時、run_pipeline の実行ログに run_id が含まれること。"""
    proc = subprocess.CompletedProcess(["mypy"], returncode=0, stdout="")
    mocker.patch("pyfltr.command._run_subprocess", return_value=proc)

    with caplog.at_level(logging.INFO, logger="pyfltr.main"):
        pyfltr.main.run(["ci", "--commands=mypy", str(pathlib.Path(__file__).parent.parent)])

    assert any("run_id:" in record.message for record in caplog.records)


def test_run_pipeline_does_not_log_run_id_when_archive_disabled(mocker, caplog, _archive_cache):
    """--no-archive 指定時は run_id ログを出力しないこと。"""
    proc = subprocess.CompletedProcess(["mypy"], returncode=0, stdout="")
    mocker.patch("pyfltr.command._run_subprocess", return_value=proc)

    with caplog.at_level(logging.INFO, logger="pyfltr.main"):
        pyfltr.main.run(["ci", "--no-archive", "--commands=mypy", str(pathlib.Path(__file__).parent.parent)])

    assert not any("run_id:" in record.message for record in caplog.records)


# --- precommit MM 状態ガイダンス ---


def _make_formatted_result() -> pyfltr.command.CommandResult:
    """``status == "formatted"`` になる最小の CommandResult を生成する。"""
    return pyfltr.command.CommandResult(
        "ruff-format", "formatter", ["ruff", "format"], returncode=1, has_error=False, files=1, output="", elapsed=0.01
    )


def _make_succeeded_result() -> pyfltr.command.CommandResult:
    """``status == "succeeded"`` になる最小の CommandResult を生成する。"""
    # キーワード引数の並びを連続せず1行にまとめて pylint duplicate-code 検出を回避する。
    # 他テストファイルにも同様の最小構築があるため、文字列比較で重複と判定されやすい。
    return pyfltr.command.CommandResult(
        "mypy", "linter", ["mypy"], returncode=0, has_error=False, files=1, output="", elapsed=0.01
    )


def test_precommit_guidance_emitted_when_formatted_under_git(monkeypatch, capsys):
    """formatted 結果があり git commit 経由のときガイダンスが stderr に出る。"""
    monkeypatch.setattr(pyfltr.main.pyfltr.precommit, "is_invoked_from_git_commit", lambda: True)
    pyfltr.main._maybe_emit_precommit_guidance(
        [_make_formatted_result(), _make_succeeded_result()],
        structured_stdout=False,
    )
    captured = capsys.readouterr()
    assert "formatter" in captured.err
    assert "git add" in captured.err


def test_precommit_guidance_skipped_when_not_under_git(monkeypatch, capsys):
    """git commit 経由でなければ formatted があってもガイダンスを出さない。"""
    monkeypatch.setattr(pyfltr.main.pyfltr.precommit, "is_invoked_from_git_commit", lambda: False)
    pyfltr.main._maybe_emit_precommit_guidance(
        [_make_formatted_result()],
        structured_stdout=False,
    )
    captured = capsys.readouterr()
    assert captured.err == ""


def test_precommit_guidance_skipped_when_no_formatted(monkeypatch, capsys):
    """formatted 結果が無ければガイダンスを出さない。"""
    monkeypatch.setattr(pyfltr.main.pyfltr.precommit, "is_invoked_from_git_commit", lambda: True)
    pyfltr.main._maybe_emit_precommit_guidance(
        [_make_succeeded_result()],
        structured_stdout=False,
    )
    captured = capsys.readouterr()
    assert captured.err == ""


def test_precommit_guidance_skipped_under_structured_stdout(monkeypatch, capsys):
    """構造化 stdout モードでは stderr へ漏らさない (``captured.err == ""`` 契約保持)。"""
    monkeypatch.setattr(pyfltr.main.pyfltr.precommit, "is_invoked_from_git_commit", lambda: True)
    pyfltr.main._maybe_emit_precommit_guidance(
        [_make_formatted_result()],
        structured_stdout=True,
    )
    captured = capsys.readouterr()
    assert captured.err == ""
