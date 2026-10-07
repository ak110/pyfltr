import logging
import pathlib

import pytest

import pyfltr.cli.main
import pyfltr.cli.output_format
import pyfltr.command.only_failed
import pyfltr.output.logging_
import pyfltr.run_options
import pyfltr.state.archive
import pyfltr.state.only_failed
import pyfltr.warnings_
from tests import conftest as _testconf
from tests.conftest import make_error_location as _make_error
from tests.conftest import seed_archive_run as _seed_run

# ---------------------------------------------------------------------------
# apply_filter
# ---------------------------------------------------------------------------


def test_apply_filter_not_only_failed(_only_failed_cache: pathlib.Path) -> None:
    """only_failed=False（未指定）のとき、（commands、None、False）を返す。"""
    args = pyfltr.run_options.RunOptions.from_values({"only_failed": False})
    commands, targets, exit_early = pyfltr.state.only_failed.apply_filter(args, ["ruff-check"], [])
    assert commands == ["ruff-check"]
    assert targets is None
    assert exit_early is False


def test_apply_filter_builds_per_tool_targets(_only_failed_cache: pathlib.Path) -> None:
    """直前runの失敗ツールごとに独立したToolTargetsを構築する。"""
    _seed_run(
        _only_failed_cache,
        commands=["ruff-check", "mypy"],
        exit_code=1,
        tool_results=[
            ("ruff-check", 1, "", [_make_error("ruff-check", "a.py", 1, "e")]),
            ("mypy", 1, "", [_make_error("mypy", "b.py", 1, "e")]),
        ],
    )
    args = pyfltr.run_options.RunOptions.from_values({"only_failed": True})
    all_files = [pathlib.Path("a.py"), pathlib.Path("b.py")]
    commands, targets, exit_early = pyfltr.state.only_failed.apply_filter(args, ["ruff-check", "mypy"], all_files)

    assert exit_early is False
    assert sorted(commands) == ["mypy", "ruff-check"]
    assert targets is not None
    assert targets["ruff-check"].mode == "files"
    assert targets["ruff-check"].files == (pathlib.Path("a.py"),)
    assert targets["mypy"].mode == "files"
    assert targets["mypy"].files == (pathlib.Path("b.py"),)


def test_apply_filter_includes_resolution_failed_tools(_only_failed_cache: pathlib.Path) -> None:
    """resolution_failedのツールも--only-failedの再実行対象に含まれる。"""
    _seed_run(
        _only_failed_cache,
        commands=["shellcheck"],
        exit_code=1,
        tool_results=[("shellcheck", 1, "ツールが見つかりません", [])],
        resolution_failed_tools={"shellcheck"},
    )
    args = pyfltr.run_options.RunOptions.from_values({"only_failed": True})
    commands, targets, exit_early = pyfltr.state.only_failed.apply_filter(args, ["shellcheck"], [pathlib.Path("a.sh")])

    assert exit_early is False
    assert commands == ["shellcheck"]
    assert targets is not None
    assert "shellcheck" in targets


def test_apply_filter_fallback_for_missing_diagnostics(_only_failed_cache: pathlib.Path) -> None:
    """診断なしの失敗ツール（pass-filenames=False等）はfallbackモードになる。"""
    _seed_run(
        _only_failed_cache,
        commands=["pytest"],
        exit_code=1,
        tool_results=[("pytest", 1, "test failed", [])],
    )
    args = pyfltr.run_options.RunOptions.from_values({"only_failed": True})
    commands, targets, exit_early = pyfltr.state.only_failed.apply_filter(args, ["pytest"], [pathlib.Path("tests/t.py")])

    assert exit_early is False
    assert commands == ["pytest"]
    assert targets is not None
    assert targets["pytest"].mode == "fallback"


def test_apply_filter_skip_for_empty_targets_intersection(_only_failed_cache: pathlib.Path) -> None:
    """診断はあるがtargets交差が空のツールは除外され、全ツールで空なら早期終了する。"""
    _seed_run(
        _only_failed_cache,
        commands=["ruff-check"],
        exit_code=1,
        tool_results=[
            ("ruff-check", 1, "", [_make_error("ruff-check", "b.py", 1, "e")]),
        ],
    )
    args = pyfltr.run_options.RunOptions.from_values({"only_failed": True})
    # all_files（targets由来）にb.pyが含まれない → 交差空で早期終了
    _, _, exit_early = pyfltr.state.only_failed.apply_filter(args, ["ruff-check"], [pathlib.Path("a.py")])
    assert exit_early is True


def test_apply_filter_per_tool_exclusion_logs_reason(
    _only_failed_cache: pathlib.Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """複数ツール中1ツールのみ交差空のとき、除外されたツールについて理由が INFO ログに出力される。"""
    _seed_run(
        _only_failed_cache,
        commands=["ruff-check", "mypy"],
        exit_code=1,
        tool_results=[
            ("ruff-check", 1, "", [_make_error("ruff-check", "a.py", 1, "e")]),
            ("mypy", 1, "", [_make_error("mypy", "b.py", 1, "e")]),
        ],
    )
    monkeypatch.setattr(pyfltr.output.logging_.text_logger, "propagate", True)
    args = pyfltr.run_options.RunOptions.from_values({"only_failed": True})
    all_files = [pathlib.Path("a.py")]  # b.py は含まない → mypy のみ交差空
    with caplog.at_level(logging.INFO, logger="pyfltr.textout"):
        commands, targets, exit_early = pyfltr.state.only_failed.apply_filter(args, ["ruff-check", "mypy"], all_files)
    assert exit_early is False
    assert commands == ["ruff-check"]
    assert targets is not None and "mypy" not in targets
    assert "mypy:" in caplog.text
    assert "交差しません" in caplog.text


def test_apply_filter_early_exit_no_runs(_only_failed_cache: pathlib.Path) -> None:
    """直前runが存在しない場合はexit_early=True（commandsは未変更で返す）。"""
    args = pyfltr.run_options.RunOptions.from_values({"only_failed": True})
    commands, targets, exit_early = pyfltr.state.only_failed.apply_filter(args, ["ruff-check"], [pathlib.Path("a.py")])
    assert exit_early is True
    assert commands == ["ruff-check"]
    assert targets is None


def test_apply_filter_early_exit_no_failures(_only_failed_cache: pathlib.Path) -> None:
    """直前runが全成功ならexit_early=True（失敗ツール抽出が空）。"""
    _seed_run(
        _only_failed_cache,
        commands=["ruff-check"],
        exit_code=0,
        tool_results=[("ruff-check", 0, "", [])],
    )
    args = pyfltr.run_options.RunOptions.from_values({"only_failed": True})
    _, _, exit_early = pyfltr.state.only_failed.apply_filter(args, ["ruff-check"], [pathlib.Path("a.py")])
    assert exit_early is True


def test_apply_filter_intersects_with_targets(_only_failed_cache: pathlib.Path) -> None:
    """失敗ファイルとall_files（位置引数targets由来）の交差が対象になる。"""
    _seed_run(
        _only_failed_cache,
        commands=["ruff-check"],
        exit_code=1,
        tool_results=[
            (
                "ruff-check",
                1,
                "",
                [
                    _make_error("ruff-check", "a.py", 1, "e"),
                    _make_error("ruff-check", "b.py", 2, "e"),
                ],
            ),
        ],
    )
    args = pyfltr.run_options.RunOptions.from_values({"only_failed": True})
    all_files = [pathlib.Path("a.py")]  # targets指定でb.pyは含まない想定
    _, targets, exit_early = pyfltr.state.only_failed.apply_filter(args, ["ruff-check"], all_files)

    assert exit_early is False
    assert targets is not None
    assert targets["ruff-check"].mode == "files"
    assert targets["ruff-check"].files == (pathlib.Path("a.py"),)


# ---------------------------------------------------------------------------
# apply_filter: --from-run オプション
# ---------------------------------------------------------------------------


def test_apply_filter_from_run_full_id(_only_failed_cache: pathlib.Path) -> None:
    """from_runに完全なrun_idを渡すと正しく解決される。"""
    run_id = _seed_run(
        _only_failed_cache,
        commands=["ruff-check"],
        exit_code=1,
        tool_results=[("ruff-check", 1, "", [_make_error("ruff-check", "a.py", 1, "e")])],
    )
    args = pyfltr.run_options.RunOptions.from_values({"only_failed": True})
    _, targets, exit_early = pyfltr.state.only_failed.apply_filter(
        args, ["ruff-check"], [pathlib.Path("a.py")], from_run=run_id
    )
    assert exit_early is False
    assert targets is not None
    assert "ruff-check" in targets


def test_apply_filter_from_run_prefix(_only_failed_cache: pathlib.Path) -> None:
    """from_runに前方一致プレフィックスを渡すと解決される。"""
    run_id = _seed_run(
        _only_failed_cache,
        commands=["ruff-check"],
        exit_code=1,
        tool_results=[("ruff-check", 1, "", [_make_error("ruff-check", "a.py", 1, "e")])],
    )
    prefix = run_id[:8]
    args = pyfltr.run_options.RunOptions.from_values({"only_failed": True})
    _, targets, exit_early = pyfltr.state.only_failed.apply_filter(
        args, ["ruff-check"], [pathlib.Path("a.py")], from_run=prefix
    )
    assert exit_early is False
    assert targets is not None
    assert "ruff-check" in targets


def test_apply_filter_from_run_latest(_only_failed_cache: pathlib.Path) -> None:
    """from_run="latest"エイリアスで最新runを参照できる。"""
    _seed_run(_only_failed_cache, commands=["ruff-check"], exit_code=0)
    _seed_run(
        _only_failed_cache,
        commands=["ruff-check"],
        exit_code=1,
        tool_results=[("ruff-check", 1, "", [_make_error("ruff-check", "a.py", 1, "e")])],
    )
    args = pyfltr.run_options.RunOptions.from_values({"only_failed": True})
    _, targets, exit_early = pyfltr.state.only_failed.apply_filter(
        args, ["ruff-check"], [pathlib.Path("a.py")], from_run="latest"
    )
    assert exit_early is False
    assert targets is not None


def test_apply_filter_from_run_not_found(_only_failed_cache: pathlib.Path, caplog: pytest.LogCaptureFixture) -> None:
    """存在しないrun_idを指定するとwarningログを発行して早期終了する。"""
    args = pyfltr.run_options.RunOptions.from_values({"only_failed": True})
    with caplog.at_level(logging.WARNING, logger="pyfltr.state.only_failed"):
        commands, targets, exit_early = pyfltr.state.only_failed.apply_filter(
            args, ["ruff-check"], [pathlib.Path("a.py")], from_run="nonexistent-run-id"
        )
    assert exit_early is True
    assert targets is None
    assert commands == ["ruff-check"]
    assert "--from-run" in caplog.text


def test_apply_filter_from_run_ambiguous_prefix(_only_failed_cache: pathlib.Path, caplog: pytest.LogCaptureFixture) -> None:
    """曖昧なプレフィックスを指定するとwarningを発行して早期終了する。"""
    # 2件以上runを生成して共通プレフィックスを特定する
    run_ids = [_seed_run(_only_failed_cache) for _ in range(3)]
    # 共通プレフィックスを算出する
    shared = _testconf.shared_prefix_length(run_ids[0], run_ids[1])
    if shared < 1:
        pytest.skip("共通プレフィックスが無いケースは曖昧判定にならない")
    prefix = run_ids[0][:shared]

    args = pyfltr.run_options.RunOptions.from_values({"only_failed": True})
    with caplog.at_level(logging.WARNING, logger="pyfltr.state.only_failed"):
        _, targets, exit_early = pyfltr.state.only_failed.apply_filter(
            args, ["ruff-check"], [pathlib.Path("a.py")], from_run=prefix
        )
    assert exit_early is True
    assert targets is None
    assert "--from-run" in caplog.text


def test_apply_filter_archive_unreadable_guides_permission_and_full_run(
    _only_failed_cache: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """実行アーカイブを読めない場合は、スキップした理由と権限確認・全体実行の案内を警告する。"""

    def _raise(*_args: object, **_kwargs: object) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(pyfltr.state.archive.ArchiveStore, "__init__", _raise)
    args = pyfltr.run_options.RunOptions.from_values({"only_failed": True})
    _, _, exit_early = pyfltr.state.only_failed.apply_filter(args, ["ruff-check"], [pathlib.Path("a.py")])
    assert exit_early is True
    warnings = [w for w in pyfltr.warnings_.collected_warnings() if w["source"] == "only-failed"]
    assert len(warnings) == 1
    assert "スキップしました" in warnings[0]["message"]
    assert "読み取り権限を確認してください" in warnings[0]["hint"]
    assert "--only-failed を外して全体を実行" in warnings[0]["hint"]
