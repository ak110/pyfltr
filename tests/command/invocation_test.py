import pytest

import pyfltr.command.core_
import pyfltr.command.dispatcher
import pyfltr.command.env
import pyfltr.command.glab
import pyfltr.command.invocation
import pyfltr.command.markdownlint_env
import pyfltr.command.mise
import pyfltr.command.only_failed
import pyfltr.command.process
import pyfltr.command.runner
import pyfltr.command.slow_tests
import pyfltr.command.subprojects
import pyfltr.command.targets
import pyfltr.command.tool_resolution
import pyfltr.command.tool_version
import pyfltr.command.two_step.base
import pyfltr.command.two_step.prettier
import pyfltr.command.two_step.ruff
import pyfltr.config.config
import pyfltr.config.model
import pyfltr.paths
import pyfltr.run_options
import pyfltr.state.cache
import pyfltr.state.only_failed
import pyfltr.tools
import pyfltr.warnings_


def test_expanduser_expands_args_in_build_invocation_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    """`{command}-args` 各要素の `~` がbuild_invocation_argvで展開される。

    展開規則は2点。要素先頭の `~` / `~user` を `os.path.expanduser` で展開する。
    要素内の最初の `=` 直後の `~` も展開する（`--config=~/cfg.toml` 形式の利便のため）。
    """
    # Windowsの`ntpath.expanduser`は`USERPROFILE`を優先するため両方上書きする。
    monkeypatch.setenv("HOME", "/tmp/fake-home")
    monkeypatch.setenv("USERPROFILE", "/tmp/fake-home")
    config = pyfltr.config.config.create_default_config()
    config.values["mypy-args"] = ["--config", "~/cfg.toml", "~/extra/path.py", "--cache-dir=~/cache"]
    argv = pyfltr.command.invocation.build_invocation_argv(
        "mypy", config, commandline_prefix=["mypy"], additional_args=[], fix_stage=False
    )
    assert "/tmp/fake-home/cfg.toml" in argv
    assert "/tmp/fake-home/extra/path.py" in argv
    assert "--cache-dir=/tmp/fake-home/cache" in argv


def test_expanduser_expands_fix_args(monkeypatch: pytest.MonkeyPatch) -> None:
    """`{command}-fix-args` 各要素の `~` がfix_stage=Trueの組み立て時に展開される。"""
    # Windowsの`ntpath.expanduser`は`USERPROFILE`を優先するため両方上書きする。
    monkeypatch.setenv("HOME", "/tmp/fake-home")
    monkeypatch.setenv("USERPROFILE", "/tmp/fake-home")
    config = pyfltr.config.config.create_default_config()
    config.values["ruff-check-fix-args"] = ["--fix", "--config", "~/ruff.toml"]
    argv = pyfltr.command.invocation.build_invocation_argv(
        "ruff-check", config, commandline_prefix=["ruff"], additional_args=[], fix_stage=True
    )
    assert "/tmp/fake-home/ruff.toml" in argv


def test_expanduser_expands_lint_args(monkeypatch: pytest.MonkeyPatch) -> None:
    """`{command}-lint-args` 各要素の `~` がfix-args未指定の通常段で展開される。

    fix_stage=Trueでもfix-args未定義のコマンドではlint-argsが結合経路に残る。
    """
    monkeypatch.setenv("HOME", "/tmp/fake-home")
    monkeypatch.setenv("USERPROFILE", "/tmp/fake-home")
    config = pyfltr.config.config.create_default_config()
    config.values["mypy-lint-args"] = ["--strict", "--config=~/strict.toml"]
    argv = pyfltr.command.invocation.build_invocation_argv(
        "mypy", config, commandline_prefix=["mypy"], additional_args=[], fix_stage=False
    )
    assert "--config=/tmp/fake-home/strict.toml" in argv


def test_build_invocation_argv_without_extend_args() -> None:
    """`{command}-extend-args`未設定時は従来と同じargvを返す。"""
    config = pyfltr.config.config.create_default_config()
    # mypyのauto_args（mypy-unused-awaitable）を無効化し、検証対象を{command}-argsの結合に限定する
    config.values["mypy-unused-awaitable"] = False
    config.values["mypy-args"] = ["--strict"]
    argv = pyfltr.command.invocation.build_invocation_argv(
        "mypy", config, commandline_prefix=["mypy"], additional_args=["-p", "pkg"], fix_stage=False
    )
    assert argv == ["mypy", "--strict", "-p", "pkg"]


def test_build_invocation_argv_appends_extend_args() -> None:
    """`{command}-extend-args`は`{command}-args`の直後に結合される。"""
    config = pyfltr.config.config.create_default_config()
    config.values["mypy-unused-awaitable"] = False
    config.values["mypy-args"] = ["--strict"]
    config.values["mypy-extend-args"] = ["--no-warn-unused-ignores"]
    argv = pyfltr.command.invocation.build_invocation_argv(
        "mypy", config, commandline_prefix=["mypy"], additional_args=["-p", "pkg"], fix_stage=False
    )
    assert argv == ["mypy", "--strict", "--no-warn-unused-ignores", "-p", "pkg"]


@pytest.mark.parametrize(
    ("configured_args", "expected_prefix"),
    [
        (["--format", "text"], ["arid", "--project-root", "."]),
        (["--format=text"], ["arid", "--project-root", "."]),
        (["--json", "--project-root", "src"], ["arid", "--project-root", ".", "--project-root", "src"]),
    ],
)
def test_build_invocation_argv_applies_arid_json(configured_args: list[str], expected_prefix: list[str]) -> None:
    """aridの競合する出力指定を除去し、JSON形式を1件だけ注入する。"""
    config = pyfltr.config.config.create_default_config()
    config.values["arid-args"] = ["--project-root", ".", *configured_args]

    argv = pyfltr.command.invocation.build_invocation_argv(
        "arid", config, commandline_prefix=["arid"], additional_args=[], fix_stage=False
    )

    assert argv == [*expected_prefix, "--format=json"]


def test_build_invocation_argv_fix_stage_includes_extend_args() -> None:
    """fix段でも`{command}-extend-args`が`args`直後に結合される。"""
    config = pyfltr.config.config.create_default_config()
    # ruff-check-json由来の--output-format=json注入は別経路の検査対象のため、ここでは無効化する
    config.values["ruff-check-json"] = False
    config.values["ruff-check-args"] = ["check"]
    config.values["ruff-check-extend-args"] = ["--config=pyproject.toml"]
    argv = pyfltr.command.invocation.build_invocation_argv(
        "ruff-check", config, commandline_prefix=["ruff"], additional_args=[], fix_stage=True
    )
    # ruff-check-fix-argsは既定で ["--fix", "--unsafe-fixes"]
    assert argv == ["ruff", "check", "--config=pyproject.toml", "--fix", "--unsafe-fixes"]


def test_expanduser_expands_extend_args(monkeypatch: pytest.MonkeyPatch) -> None:
    """`{command}-extend-args`各要素の`~`がbuild_invocation_argvで展開される。"""
    monkeypatch.setenv("HOME", "/tmp/fake-home")
    monkeypatch.setenv("USERPROFILE", "/tmp/fake-home")
    config = pyfltr.config.config.create_default_config()
    config.values["mypy-extend-args"] = ["--config=~/mypy.toml"]
    argv = pyfltr.command.invocation.build_invocation_argv(
        "mypy", config, commandline_prefix=["mypy"], additional_args=[], fix_stage=False
    )
    assert "--config=/tmp/fake-home/mypy.toml" in argv


def test_build_invocation_argv_auto_args_pylint_pydantic() -> None:
    """pylint-pydantic=trueの場合に自動引数が挿入される。"""
    config = pyfltr.config.config.create_default_config()
    argv = pyfltr.command.invocation.build_invocation_argv(
        "pylint", config, commandline_prefix=["pylint"], additional_args=[], fix_stage=False
    )
    assert "--load-plugins=pylint_pydantic" in argv


def test_build_invocation_argv_auto_args_mypy_unused_awaitable() -> None:
    """mypy-unused-awaitable=trueの場合に自動引数が挿入される。"""
    config = pyfltr.config.config.create_default_config()
    argv = pyfltr.command.invocation.build_invocation_argv(
        "mypy", config, commandline_prefix=["mypy"], additional_args=[], fix_stage=False
    )
    assert "--enable-error-code=unused-awaitable" in argv


def test_build_invocation_argv_auto_value_args_lychee() -> None:
    """lychee-max-concurrencyの既定値が`--max-concurrency=4`として挿入される。"""
    config = pyfltr.config.config.create_default_config()
    argv = pyfltr.command.invocation.build_invocation_argv(
        "lychee", config, commandline_prefix=["lychee"], additional_args=[], fix_stage=False
    )
    assert "--max-concurrency=4" in argv


def test_build_invocation_argv_auto_value_args_disabled() -> None:
    """lychee-max-concurrencyが0以下なら`--max-concurrency`を挿入しない。"""
    config = pyfltr.config.config.create_default_config()
    config.values["lychee-max-concurrency"] = 0
    argv = pyfltr.command.invocation.build_invocation_argv(
        "lychee", config, commandline_prefix=["lychee"], additional_args=[], fix_stage=False
    )
    assert not any(arg.startswith("--max-concurrency") for arg in argv)


def test_build_invocation_argv_auto_value_args_user_override() -> None:
    """利用者が`--max-concurrency`を指定済みなら自動追加しない。"""
    config = pyfltr.config.config.create_default_config()
    config.values["lychee-extend-args"] = ["--max-concurrency=16"]
    argv = pyfltr.command.invocation.build_invocation_argv(
        "lychee", config, commandline_prefix=["lychee"], additional_args=[], fix_stage=False
    )
    assert [arg for arg in argv if arg.startswith("--max-concurrency")] == ["--max-concurrency=16"]


def test_build_invocation_argv_auto_args_disabled() -> None:
    """自動オプションをfalseにすると引数が挿入されない。"""
    config = pyfltr.config.config.create_default_config()
    config.values["pylint-pydantic"] = False
    argv = pyfltr.command.invocation.build_invocation_argv(
        "pylint", config, commandline_prefix=["pylint"], additional_args=[], fix_stage=False
    )
    assert "--load-plugins=pylint_pydantic" not in argv


def test_build_invocation_argv_auto_args_dedup_with_user_args() -> None:
    """ユーザーが既に同じ引数を指定している場合はスキップする。"""
    config = pyfltr.config.config.create_default_config()
    config.values["pylint-args"] = ["--load-plugins=pylint_pydantic", "--jobs=4"]
    argv = pyfltr.command.invocation.build_invocation_argv(
        "pylint", config, commandline_prefix=["pylint"], additional_args=[], fix_stage=False
    )
    # pylint-args設定値に含まれるため、自動引数による重複挿入がされないことを確認する
    assert argv.count("--load-plugins=pylint_pydantic") == 1


def test_build_invocation_argv_auto_args_no_match() -> None:
    """`AUTO_ARGS`に定義されていないコマンドは自動引数が挿入されない。"""
    config = pyfltr.config.config.create_default_config()
    # ruff-check-jsonを無効化して余分な--output-formatが入らないようにする
    config.values["ruff-check-json"] = False
    argv = pyfltr.command.invocation.build_invocation_argv(
        "ruff-check", config, commandline_prefix=["ruff"], additional_args=[], fix_stage=False
    )
    # ruff-checkはAUTO_ARGS未定義であり、AUTO_ARGS由来フラグが含まれないことを確認する
    assert "ruff-check" not in pyfltr.tools.AUTO_ARGS
    all_auto_args_flags = [flag for entries in pyfltr.tools.AUTO_ARGS.values() for _, args in entries for flag in args]
    for flag in all_auto_args_flags:
        assert not any(a == flag or a.startswith(flag + "=") for a in argv), (
            f"AUTO_ARGS由来フラグ {flag} が意図せず含まれています"
        )


def test_build_invocation_argv_disables_textlint_cache_only_in_fix_stage() -> None:
    """textlintの修正段は利用者引数の後ろへ`--no-cache`を置き、通常段は利用者のキャッシュ指定を保つ。"""
    config = pyfltr.config.config.create_default_config()
    config.values["textlint-args"] = ["--cache"]

    fix_argv = pyfltr.command.invocation.build_invocation_argv("textlint", config, ["textlint"], ["--quiet"], fix_stage=True)
    lint_argv = pyfltr.command.invocation.build_invocation_argv("textlint", config, ["textlint"], [], fix_stage=False)

    assert fix_argv[-1] == "--no-cache"
    assert fix_argv.index("--cache") < fix_argv.index("--quiet") < fix_argv.index("--no-cache")
    assert "--cache" in lint_argv
    assert "--no-cache" not in lint_argv
