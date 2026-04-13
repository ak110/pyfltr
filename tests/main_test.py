# pylint: disable=missing-module-docstring
# pylint: disable=missing-function-docstring
# pylint: disable=protected-access

import pathlib
import subprocess

import pytest

import pyfltr.main


@pytest.mark.parametrize("mode", ["run", "ci", "pre-commit"])
def test_success(mocker, mode):
    proc = subprocess.CompletedProcess(["test"], returncode=0, stdout="test")
    mocker.patch("subprocess.run", return_value=proc)
    returncode = pyfltr.main.run([mode, str(pathlib.Path(__file__).parent.parent)])
    assert returncode == 0


@pytest.mark.parametrize("mode", ["run", "ci", "pre-commit"])
def test_fail(mocker, mode):
    proc = subprocess.CompletedProcess(["test"], returncode=-1, stdout="test")
    mocker.patch("subprocess.run", return_value=proc)
    returncode = pyfltr.main.run([mode, str(pathlib.Path(__file__).parent.parent)])
    assert returncode == 1


def test_work_dir(mocker, tmp_path):
    """--work-dirオプションのテスト。"""
    (tmp_path / "pyproject.toml").write_text('[tool.pyfltr]\npreset = "latest"\n')
    proc = subprocess.CompletedProcess(["test"], returncode=0, stdout="test")
    mocker.patch("subprocess.run", return_value=proc)
    original_cwd = pathlib.Path.cwd()
    returncode = pyfltr.main.run(["--work-dir", str(tmp_path), "--commands=pytest", str(pathlib.Path(__file__).parent.parent)])
    assert returncode == 0
    # cwdが復元されていることを確認
    assert pathlib.Path.cwd() == original_cwd


def test_fix_mode_with_no_eligible_commands(mocker, caplog):
    """fix モードで対象コマンドが 0 件なら exit 1 にする。"""
    # formatter だけ指定しても fix 対象は 0 件になる (formatter は対象外)
    proc = subprocess.CompletedProcess(["test"], returncode=0, stdout="")
    mocker.patch("subprocess.run", return_value=proc)
    returncode = pyfltr.main.run(["fix", "--commands=black,ruff-format", str(pathlib.Path(__file__).parent.parent)])
    # black/ruff-format は formatter のため 0 件
    assert returncode == 1
    assert "fix モードで実行可能なコマンドがありません" in caplog.text


def test_fix_mode_disables_shuffle(mocker, caplog):
    """fix モードと --shuffle を同時指定した場合、shuffle が無効化される。"""
    proc = subprocess.CompletedProcess(["test"], returncode=0, stdout="")
    mocker.patch("subprocess.run", return_value=proc)
    # ruff-check は fix-args 定義済みかつ preset=latest で有効化されている
    returncode = pyfltr.main.run(["fix", "--shuffle", "--commands=ruff-check", str(pathlib.Path(__file__).parent.parent)])
    assert returncode == 0
    assert "--shuffle を無効化" in caplog.text


def test_explicit_fix_flag_emits_deprecation_warning(mocker, caplog):
    """`--fix` を明示指定すると非推奨警告が出力される。"""
    proc = subprocess.CompletedProcess(["test"], returncode=0, stdout="")
    mocker.patch("subprocess.run", return_value=proc)
    pyfltr.main.run(["--fix", "--commands=ruff-check", str(pathlib.Path(__file__).parent.parent)])
    assert "--fix は非推奨です" in caplog.text


def test_fix_subcommand_does_not_emit_deprecation_warning(mocker, caplog):
    """`pyfltr fix` サブコマンド経由では非推奨警告は出力されない。"""
    proc = subprocess.CompletedProcess(["test"], returncode=0, stdout="")
    mocker.patch("subprocess.run", return_value=proc)
    pyfltr.main.run(["fix", "--commands=ruff-check", str(pathlib.Path(__file__).parent.parent)])
    assert "--fix は非推奨です" not in caplog.text


def test_stream_mode_writes_detail_log_during_run(mocker, caplog):
    """--stream 指定時はコマンド完了時に詳細ログが出力される。"""
    proc = subprocess.CompletedProcess(["mypy"], returncode=0, stdout="mypy-detail")
    mocker.patch("subprocess.run", return_value=proc)

    returncode = pyfltr.main.run(["--no-ui", "--stream", "--commands=mypy", str(pathlib.Path(__file__).parent.parent)])
    assert returncode == 0
    # 詳細ログに含まれる returncode 行が出力される
    assert "returncode: 0" in caplog.text
    # summary セクションも引き続き出力される
    assert "summary" in caplog.text


def test_buffered_mode_is_default(mocker, caplog):
    """既定では 成功コマンド詳細 → summary の順でまとめて出力される。"""
    proc = subprocess.CompletedProcess(["mypy"], returncode=0, stdout="mypy-detail")
    mocker.patch("subprocess.run", return_value=proc)

    returncode = pyfltr.main.run(["--no-ui", "--commands=mypy", str(pathlib.Path(__file__).parent.parent)])
    assert returncode == 0
    text = caplog.text
    # 詳細ログが summary より先に来る (summary は末尾)
    assert "summary" in text
    assert "returncode: 0" in text
    assert text.index("returncode: 0") < text.index("summary")


def test_additional_args(mocker):
    """追加引数のテスト。"""
    proc = subprocess.CompletedProcess(["pytest"], returncode=0, stdout="test")
    mock_run = mocker.patch("subprocess.run", return_value=proc)

    returncode = pyfltr.main.run(
        ["--commands=pytest", "--pytest-args=--maxfail=5 -v", str(pathlib.Path(__file__).parent.parent)]
    )
    assert returncode == 0

    # subprocess.runが呼ばれた引数を確認
    assert mock_run.called
    called_args = mock_run.call_args[0][0]  # 最初の引数（コマンドライン）
    assert "--maxfail=5" in called_args
    assert "-v" in called_args


class TestParseSubcommand:
    """_parse_subcommandのテスト。"""

    def test_explicit_ci(self):
        sub, remaining = pyfltr.main._parse_subcommand(["ci", "src/"])
        assert sub == "ci"
        assert remaining == ["src/"]

    def test_implicit_ci(self):
        """予約語以外の第一引数はciとして扱う。"""
        sub, remaining = pyfltr.main._parse_subcommand(["--verbose", "src/"])
        assert sub == "ci"
        assert remaining == ["--verbose", "src/"]

    def test_run_subcommand(self):
        sub, remaining = pyfltr.main._parse_subcommand(["run", "src/"])
        assert sub == "run"
        assert remaining == ["src/"]

    def test_fast_subcommand(self):
        sub, remaining = pyfltr.main._parse_subcommand(["fast", "src/"])
        assert sub == "fast"
        assert remaining == ["src/"]

    def test_fix_subcommand(self):
        sub, remaining = pyfltr.main._parse_subcommand(["fix", "src/"])
        assert sub == "fix"
        assert remaining == ["src/"]

    def test_dirty_subcommand_is_deprecated(self):
        """廃止されたdirtyサブコマンドがエラー終了することを確認。"""
        assert pyfltr.main.run(["dirty", "init"]) == 1

    def test_empty_args(self):
        sub, remaining = pyfltr.main._parse_subcommand([])
        assert sub == "ci"
        assert not remaining


class TestBuildEffectiveArgs:
    """_build_effective_argsのテスト。"""

    def test_ci(self):
        result = pyfltr.main._build_effective_args("ci", ["src/"])
        assert result == ["src/"]

    def test_run(self):
        result = pyfltr.main._build_effective_args("run", ["src/"])
        assert result == ["--exit-zero-even-if-formatted", "src/"]

    def test_fast(self):
        result = pyfltr.main._build_effective_args("fast", ["src/"])
        assert result == ["--exit-zero-even-if-formatted", "--commands=fast", "src/"]

    def test_fix(self):
        result = pyfltr.main._build_effective_args("fix", ["src/"])
        assert result == ["--fix", "src/"]


class TestSubcommandIntegration:
    """サブコマンドの統合テスト。"""

    def test_run_subcommand(self, mocker):
        """runサブコマンドで--exit-zero-even-if-formattedが暗黙的に有効化される。"""
        proc = subprocess.CompletedProcess(["test"], returncode=0, stdout="test")
        mocker.patch("subprocess.run", return_value=proc)
        returncode = pyfltr.main.run(["run", str(pathlib.Path(__file__).parent.parent)])
        assert returncode == 0

    def test_fast_subcommand(self, mocker):
        """fastサブコマンドで--exit-zero-even-if-formattedと--commands=fastが暗黙的に有効化される。"""
        proc = subprocess.CompletedProcess(["test"], returncode=0, stdout="test")
        mocker.patch("subprocess.run", return_value=proc)
        returncode = pyfltr.main.run(["fast", str(pathlib.Path(__file__).parent.parent)])
        assert returncode == 0

    def test_ci_explicit(self, mocker):
        """明示的なciサブコマンド。"""
        proc = subprocess.CompletedProcess(["test"], returncode=0, stdout="test")
        mocker.patch("subprocess.run", return_value=proc)
        returncode = pyfltr.main.run(["ci", str(pathlib.Path(__file__).parent.parent)])
        assert returncode == 0

    def test_run_includes_custom_commands_by_default(self, mocker, tmp_path):
        """`run` サブコマンドのデフォルトで custom-commands も実行される。"""
        pyproject = """
[tool.pyfltr]
python = false
eslint = false
prettier = false
markdownlint = false
textlint = false
biome = false
oxlint = false
tsc = false
vitest = false
ec = false
shellcheck = false
typos = false
actionlint = false

[tool.pyfltr.custom-commands.my-linter]
type = "linter"
path = "my-linter-exe"
targets = ["*.py"]
pass-filenames = false
"""
        (tmp_path / "pyproject.toml").write_text(pyproject)
        (tmp_path / "sample.py").write_text("x = 1\n")

        proc = subprocess.CompletedProcess(["my-linter-exe"], returncode=0, stdout="")
        mock_run = mocker.patch("subprocess.run", return_value=proc)

        returncode = pyfltr.main.run(["run", "--work-dir", str(tmp_path), str(tmp_path)])
        assert returncode == 0

        invoked_binaries = {
            call.args[0][0] for call in mock_run.call_args_list if call.args and isinstance(call.args[0], list) and call.args[0]
        }
        assert "my-linter-exe" in invoked_binaries
