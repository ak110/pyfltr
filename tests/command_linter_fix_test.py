"""command.py の linter fix テスト。

``_execute_linter_fix`` の動作と、fix モードでの各 linter (eslint/biome/cargo/dotnet) の
コマンドライン生成を検証する。
"""

# pylint: disable=protected-access,duplicate-code

import os
import pathlib
import subprocess

import pyfltr.command
import pyfltr.config
from tests import conftest as _testconf


def test_fix_mode_appends_fix_args_for_linter(mocker, tmp_path: pathlib.Path) -> None:
    """fix モード時、linter のコマンドラインに fix-args が追加される。"""
    target = tmp_path / "sample.md"
    target.write_text("# title\n")

    proc = subprocess.CompletedProcess(["ruff"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command._run_subprocess", return_value=proc)

    config = pyfltr.config.create_default_config()
    config.values["markdownlint"] = True
    result = pyfltr.command.execute_command(
        "markdownlint", _testconf.make_args(), _testconf.make_execution_context(config, [target], fix_stage=True)
    )

    assert mock_run.call_count == 1
    cmdline = mock_run.call_args_list[0][0][0]
    # 通常 args ("markdownlint-cli2") の後に fix-args ("--fix") が続く
    assert "markdownlint-cli2" in cmdline
    assert "--fix" in cmdline
    assert cmdline.index("markdownlint-cli2") < cmdline.index("--fix")
    # 変更なし + rc=0 なので succeeded
    assert result.status == "succeeded"


def test_fix_mode_preserves_custom_args(mocker, tmp_path: pathlib.Path) -> None:
    """プロジェクトが上書きした {command}-args が fix モードでも保持される (置換されない)。

    markdownlint は単発 fix 経路を通るため、通常 args の後に fix-args が append される。
    """
    target = tmp_path / "sample.md"
    target.write_text("# title\n")

    proc = subprocess.CompletedProcess(["markdownlint-cli2"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command._run_subprocess", return_value=proc)

    config = pyfltr.config.create_default_config()
    config.values["markdownlint"] = True
    config.values["markdownlint-args"] = ["--config", "custom.yaml"]
    pyfltr.command.execute_command(
        "markdownlint", _testconf.make_args(), _testconf.make_execution_context(config, [target], fix_stage=True)
    )

    cmdline = mock_run.call_args_list[0][0][0]
    # 通常 args が残っている
    assert "--config" in cmdline
    assert "custom.yaml" in cmdline
    # fix-args も追加されている
    assert "--fix" in cmdline
    # 順序: 通常 args は --fix より前
    assert cmdline.index("custom.yaml") < cmdline.index("--fix")


def test_fix_mode_mtime_change_marks_formatted(mocker, tmp_path: pathlib.Path) -> None:
    """fix モードで linter がファイルを書き換えた場合、formatted 扱いになる。"""
    target = tmp_path / "sample.md"
    target.write_text("# title\n")
    os.utime(target, (1000000000, 1000000000))

    def fake_run(cmdline, env, on_output, **_kwargs):
        del env, on_output  # noqa
        # fix 適用をシミュレート
        target.write_text("# Title\n")
        os.utime(target, (2000000000, 2000000000))
        return subprocess.CompletedProcess(cmdline, returncode=0, stdout="")

    mocker.patch("pyfltr.command._run_subprocess", side_effect=fake_run)

    config = pyfltr.config.create_default_config()
    config.values["markdownlint"] = True
    result = pyfltr.command.execute_command(
        "markdownlint", _testconf.make_args(), _testconf.make_execution_context(config, [target], fix_stage=True)
    )

    assert result.status == "formatted"
    assert result.has_error is False


def test_fix_mode_non_zero_rc_is_failed(mocker, tmp_path: pathlib.Path) -> None:
    """fix モードで rc != 0 なら mtime に関係なく failed。"""
    # ruff-check の targets は *.py
    target = tmp_path / "sample.py"
    target.write_text("x = 1\n")
    os.utime(target, (1000000000, 1000000000))

    def fake_run(cmdline, env, on_output, **_kwargs):
        del env, on_output  # noqa
        # 一部修正したが未修正の違反が残って rc=1 のケースをシミュレート
        target.write_text("# Title\n")
        os.utime(target, (2000000000, 2000000000))
        return subprocess.CompletedProcess(cmdline, returncode=1, stdout="violation remains")

    mocker.patch("pyfltr.command._run_subprocess", side_effect=fake_run)

    config = pyfltr.config.create_default_config()
    config.values["ruff-check"] = True
    result = pyfltr.command.execute_command(
        "ruff-check", _testconf.make_args(), _testconf.make_execution_context(config, [target], fix_stage=True)
    )

    # rc != 0 なので mtime 変化があっても failed
    assert result.status == "failed"
    assert result.has_error is True


def test_fix_mode_formatter_is_not_filtered_in(tmp_path: pathlib.Path) -> None:
    """filter_fix_commands は formatter を fix モードの対象から除外する。"""
    del tmp_path  # noqa  # fixture互換のためだけに受け取る
    config = pyfltr.config.create_default_config()
    config.values["ruff-format"] = True
    # ruff-format は formatter のため fix モードの対象外となる (fix-args 未定義)
    result = pyfltr.config.filter_fix_commands(["ruff-format"], config)
    assert not result


def test_eslint_lint_mode_uses_json_format(mocker, tmp_path: pathlib.Path) -> None:
    """eslint の通常実行で `--format json` (共通 args) が commandline に含まれる。"""
    target = tmp_path / "sample.js"
    target.write_text("var x = 1;\n")

    proc = subprocess.CompletedProcess(["eslint"], returncode=0, stdout="[]")
    mock_run = mocker.patch("pyfltr.command._run_subprocess", return_value=proc)

    config = pyfltr.config.create_default_config()
    config.values["eslint"] = True
    pyfltr.command.execute_command("eslint", _testconf.make_args(), _testconf.make_execution_context(config, [target]))

    assert mock_run.call_count == 1
    cmdline = mock_run.call_args_list[0][0][0]
    assert "--format" in cmdline
    assert "json" in cmdline
    fmt_idx = cmdline.index("--format")
    assert cmdline[fmt_idx + 1] == "json"
    # lint モードでは --fix は付かない
    assert "--fix" not in cmdline


def test_eslint_fix_mode_appends_fix_and_keeps_json(mocker, tmp_path: pathlib.Path) -> None:
    """eslint の fix モードで `--fix` が付いても `--format json` は維持される。"""
    target = tmp_path / "sample.js"
    target.write_text("var x = 1;\n")

    proc = subprocess.CompletedProcess(["eslint"], returncode=0, stdout="[]")
    mock_run = mocker.patch("pyfltr.command._run_subprocess", return_value=proc)

    config = pyfltr.config.create_default_config()
    config.values["eslint"] = True
    pyfltr.command.execute_command(
        "eslint", _testconf.make_args(), _testconf.make_execution_context(config, [target], fix_stage=True)
    )

    assert mock_run.call_count == 1
    cmdline = mock_run.call_args_list[0][0][0]
    assert "--format" in cmdline
    assert "json" in cmdline
    assert "--fix" in cmdline


def test_biome_lint_mode_uses_check_and_github_reporter(mocker, tmp_path: pathlib.Path) -> None:
    """biome の通常実行で `check` サブコマンドと `--reporter=github` が含まれる。"""
    target = tmp_path / "sample.ts"
    target.write_text("const x = 1;\n")

    proc = subprocess.CompletedProcess(["biome"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command._run_subprocess", return_value=proc)

    config = pyfltr.config.create_default_config()
    config.values["biome"] = True
    pyfltr.command.execute_command("biome", _testconf.make_args(), _testconf.make_execution_context(config, [target]))

    assert mock_run.call_count == 1
    cmdline = mock_run.call_args_list[0][0][0]
    assert "check" in cmdline
    assert "--reporter=github" in cmdline
    assert "--write" not in cmdline


def test_biome_fix_mode_appends_write_and_keeps_reporter(mocker, tmp_path: pathlib.Path) -> None:
    """biome の fix モードで `--write` が付いても `--reporter=github` は維持される。"""
    target = tmp_path / "sample.ts"
    target.write_text("const x = 1;\n")

    proc = subprocess.CompletedProcess(["biome"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command._run_subprocess", return_value=proc)

    config = pyfltr.config.create_default_config()
    config.values["biome"] = True
    pyfltr.command.execute_command(
        "biome", _testconf.make_args(), _testconf.make_execution_context(config, [target], fix_stage=True)
    )

    assert mock_run.call_count == 1
    cmdline = mock_run.call_args_list[0][0][0]
    assert "check" in cmdline
    assert "--reporter=github" in cmdline
    assert "--write" in cmdline
    # check は共通 args なので --write より前
    assert cmdline.index("check") < cmdline.index("--write")


# Rust / .NET 言語ツールの実行テスト。
# pass-filenames=False により crate / solution 全体を対象とし、
# ファイル引数がコマンドラインに渡らないことを検証する。


def test_cargo_fmt_runs_without_file_args(mocker, tmp_path: pathlib.Path) -> None:
    """cargo-fmt は pass-filenames=False のためファイル引数を渡さず、既定で書き込みモード。"""
    target = tmp_path / "sample.rs"
    target.write_text("fn main() {}\n")

    proc = subprocess.CompletedProcess(["cargo"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command._run_subprocess", return_value=proc)

    config = pyfltr.config.create_default_config()
    config.values["cargo-fmt"] = True
    pyfltr.command.execute_command("cargo-fmt", _testconf.make_args(), _testconf.make_execution_context(config, [target]))

    assert mock_run.call_count == 1
    cmdline = mock_run.call_args_list[0][0][0]
    assert cmdline == ["cargo", "fmt"]
    assert str(target) not in cmdline


def test_cargo_fmt_fix_mode_unchanged(mocker, tmp_path: pathlib.Path) -> None:
    """cargo-fmt は formatter なので --fix 指定でもコマンドラインが変わらない。"""
    target = tmp_path / "sample.rs"
    target.write_text("fn main() {}\n")

    proc = subprocess.CompletedProcess(["cargo"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command._run_subprocess", return_value=proc)

    config = pyfltr.config.create_default_config()
    config.values["cargo-fmt"] = True
    pyfltr.command.execute_command(
        "cargo-fmt", _testconf.make_args(), _testconf.make_execution_context(config, [target], fix_stage=True)
    )

    cmdline = mock_run.call_args_list[0][0][0]
    assert cmdline == ["cargo", "fmt"]


def test_cargo_clippy_normal_mode_cmdline(mocker, tmp_path: pathlib.Path) -> None:
    """cargo-clippy の非 fix モードは args + lint-args で組み立てられる。"""
    target = tmp_path / "sample.rs"
    target.write_text("fn main() {}\n")

    proc = subprocess.CompletedProcess(["cargo"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command._run_subprocess", return_value=proc)

    config = pyfltr.config.create_default_config()
    config.values["cargo-clippy"] = True
    pyfltr.command.execute_command("cargo-clippy", _testconf.make_args(), _testconf.make_execution_context(config, [target]))

    cmdline = mock_run.call_args_list[0][0][0]
    assert cmdline == _testconf.CARGO_CLIPPY_LINT_CMDLINE
    assert str(target) not in cmdline


def test_cargo_clippy_fix_mode_cmdline(mocker, tmp_path: pathlib.Path) -> None:
    """cargo-clippy の --fix モードは args + fix-args で組み立てられる。"""
    target = tmp_path / "sample.rs"
    target.write_text("fn main() {}\n")

    proc = subprocess.CompletedProcess(["cargo"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command._run_subprocess", return_value=proc)

    config = pyfltr.config.create_default_config()
    config.values["cargo-clippy"] = True
    pyfltr.command.execute_command(
        "cargo-clippy", _testconf.make_args(), _testconf.make_execution_context(config, [target], fix_stage=True)
    )

    cmdline = mock_run.call_args_list[0][0][0]
    assert cmdline == _testconf.CARGO_CLIPPY_FIX_CMDLINE
    assert str(target) not in cmdline


def test_dotnet_format_runs_without_file_args(mocker, tmp_path: pathlib.Path) -> None:
    """dotnet-format は pass-filenames=False で solution 全体を対象とする。"""
    target = tmp_path / "Sample.cs"
    target.write_text("class Sample {}\n")

    proc = subprocess.CompletedProcess(["dotnet"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command._run_subprocess", return_value=proc)

    config = pyfltr.config.create_default_config()
    config.values["dotnet-format"] = True
    pyfltr.command.execute_command("dotnet-format", _testconf.make_args(), _testconf.make_execution_context(config, [target]))

    cmdline = mock_run.call_args_list[0][0][0]
    assert cmdline == ["dotnet", "format"]
    assert str(target) not in cmdline


def test_cargo_test_skipped_when_no_rs_files(mocker) -> None:
    """.rs ファイルが対象に無いとき cargo-test はスキップされる (既存 pass-filenames=False 分岐)。"""
    mock_run = mocker.patch("pyfltr.command._run_subprocess")

    config = pyfltr.config.create_default_config()
    config.values["cargo-test"] = True
    result = pyfltr.command.execute_command("cargo-test", _testconf.make_args(), _testconf.make_execution_context(config, []))

    assert mock_run.call_count == 0
    assert result.returncode is None
    assert result.files == 0


def test_tool_exclude_filters_files(mocker, tmp_path: pathlib.Path) -> None:
    """{tool}-exclude に一致するファイルがツール実行から除外される。"""
    kept = tmp_path / "main.py"
    excluded_ = tmp_path / "gen_foo.py"
    kept.write_text("x = 1\n")
    excluded_.write_text("x = 2\n")

    proc = subprocess.CompletedProcess(["ruff"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command._run_subprocess", return_value=proc)

    config = pyfltr.config.create_default_config()
    config.values["ruff-check"] = True
    config.values["ruff-check-exclude"] = ["gen_*.py"]

    result = pyfltr.command.execute_command(
        "ruff-check", _testconf.make_args(), _testconf.make_execution_context(config, [kept, excluded_])
    )

    assert mock_run.call_count == 1
    cmdline = mock_run.call_args_list[0][0][0]
    assert str(kept) in cmdline
    assert str(excluded_) not in cmdline
    assert result.status == "succeeded"


def test_tool_exclude_disabled_by_no_exclude(mocker, tmp_path: pathlib.Path) -> None:
    """--no-exclude 指定時は {tool}-exclude が無効化される。"""
    kept = tmp_path / "main.py"
    would_be_excluded = tmp_path / "gen_foo.py"
    kept.write_text("x = 1\n")
    would_be_excluded.write_text("x = 2\n")

    proc = subprocess.CompletedProcess(["ruff"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command._run_subprocess", return_value=proc)

    config = pyfltr.config.create_default_config()
    config.values["ruff-check"] = True
    config.values["ruff-check-exclude"] = ["gen_*.py"]

    result = pyfltr.command.execute_command(
        "ruff-check", _testconf.make_args(no_exclude=True), _testconf.make_execution_context(config, [kept, would_be_excluded])
    )

    assert mock_run.call_count == 1
    cmdline = mock_run.call_args_list[0][0][0]
    # --no-exclude なので両ファイルとも渡される
    assert str(kept) in cmdline
    assert str(would_be_excluded) in cmdline
    assert result.status == "succeeded"
