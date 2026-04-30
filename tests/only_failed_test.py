"""only_failedモジュールのテスト。

`ToolTargets`dataclassの各分岐と`apply_filter`の3状態（fallback / files /
skip→commands絞り込みで除外）を検証する。
`tests/main_test.py`の`_apply_only_failed_filter`系テストをここへ移管済み。
"""

# pylint: disable=missing-function-docstring  # テストは関数docstringを省略する慣習

import argparse
import logging
import pathlib

import pytest

import pyfltr.state.only_failed
from tests import conftest as _testconf
from tests.conftest import make_error_location as _make_error
from tests.conftest import seed_archive_run as _seed_run


@pytest.fixture(name="_only_failed_cache")
def _only_failed_cache_fixture(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> pathlib.Path:
    """--only-failed テスト用に PYFLTR_CACHE_DIR を tmp_path に固定する。"""
    monkeypatch.setenv("PYFLTR_CACHE_DIR", str(tmp_path))
    return tmp_path


# ---------------------------------------------------------------------------
# ToolTargets dataclass
# ---------------------------------------------------------------------------


def test_tool_targets_fallback_default() -> None:
    """fallback_default()はmode="fallback"、files=()で生成される。"""
    t = pyfltr.state.only_failed.ToolTargets.fallback_default()
    assert t.mode == "fallback"
    assert len(t.files) == 0


def test_tool_targets_with_files(tmp_path: pathlib.Path) -> None:
    """with_files()はmode="files"、指定ファイルをtupleで保持する。"""
    f = tmp_path / "a.py"
    t = pyfltr.state.only_failed.ToolTargets.with_files([f])
    assert t.mode == "files"
    assert t.files == (f,)


def test_tool_targets_resolve_files_fallback_returns_all_files(tmp_path: pathlib.Path) -> None:
    """fallbackモードはall_filesをそのまま返す。"""
    a = tmp_path / "a.py"
    b = tmp_path / "b.py"
    t = pyfltr.state.only_failed.ToolTargets.fallback_default()
    assert t.resolve_files([a, b]) == [a, b]


def test_tool_targets_resolve_files_files_mode(tmp_path: pathlib.Path) -> None:
    """filesモードはself.filesのリストを返す。"""
    a = tmp_path / "a.py"
    b = tmp_path / "b.py"
    t = pyfltr.state.only_failed.ToolTargets.with_files([a])
    # b は含まれない
    assert t.resolve_files([a, b]) == [a]


def test_tool_targets_is_frozen() -> None:
    """frozen=Trueなのでフィールドへの代入はTypeErrorになる。"""
    t = pyfltr.state.only_failed.ToolTargets.fallback_default()
    with pytest.raises((TypeError, AttributeError)):
        # frozen=Trueへの代入が実行時にTypeErrorを送出することを検証するため、型チェッカーの警告を抑止する。
        t.mode = "files"  # type: ignore[misc]  # ty: ignore[invalid-assignment]


# ---------------------------------------------------------------------------
# apply_filter
# ---------------------------------------------------------------------------


def test_apply_filter_not_only_failed(_only_failed_cache: pathlib.Path) -> None:
    """only_failed=False（未指定）のとき、（commands、None、False）を返す。"""
    args = argparse.Namespace(only_failed=False)
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
    args = argparse.Namespace(only_failed=True)
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
    args = argparse.Namespace(only_failed=True)
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
    args = argparse.Namespace(only_failed=True)
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
    args = argparse.Namespace(only_failed=True)
    # all_files（targets由来）にb.pyが含まれない → 交差空で早期終了
    _, _, exit_early = pyfltr.state.only_failed.apply_filter(args, ["ruff-check"], [pathlib.Path("a.py")])
    assert exit_early is True


def test_apply_filter_early_exit_no_runs(_only_failed_cache: pathlib.Path) -> None:
    """直前runが存在しない場合はexit_early=True（commandsは未変更で返す）。"""
    args = argparse.Namespace(only_failed=True)
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
    args = argparse.Namespace(only_failed=True)
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
    args = argparse.Namespace(only_failed=True)
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
    args = argparse.Namespace(only_failed=True)
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
    args = argparse.Namespace(only_failed=True)
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
    args = argparse.Namespace(only_failed=True)
    _, targets, exit_early = pyfltr.state.only_failed.apply_filter(
        args, ["ruff-check"], [pathlib.Path("a.py")], from_run="latest"
    )
    assert exit_early is False
    assert targets is not None


def test_apply_filter_from_run_not_found(_only_failed_cache: pathlib.Path, caplog: pytest.LogCaptureFixture) -> None:
    """存在しないrun_idを指定するとwarningログを出して早期終了する。"""
    args = argparse.Namespace(only_failed=True)
    with caplog.at_level(logging.WARNING, logger="pyfltr.state.only_failed"):
        commands, targets, exit_early = pyfltr.state.only_failed.apply_filter(
            args, ["ruff-check"], [pathlib.Path("a.py")], from_run="nonexistent-run-id"
        )
    assert exit_early is True
    assert targets is None
    assert commands == ["ruff-check"]
    assert "--from-run" in caplog.text


def test_apply_filter_from_run_ambiguous_prefix(_only_failed_cache: pathlib.Path, caplog: pytest.LogCaptureFixture) -> None:
    """曖昧なプレフィックスを指定するとwarningを出して早期終了する。"""
    # 2件以上runを生成して共通プレフィックスを特定する
    run_ids = [_seed_run(_only_failed_cache) for _ in range(3)]
    # 共通プレフィックスを算出する
    shared = _testconf.shared_prefix_length(run_ids[0], run_ids[1])
    if shared < 1:
        pytest.skip("共通プレフィックスが無いケースは曖昧判定にならない")
    prefix = run_ids[0][:shared]

    args = argparse.Namespace(only_failed=True)
    with caplog.at_level(logging.WARNING, logger="pyfltr.state.only_failed"):
        _, targets, exit_early = pyfltr.state.only_failed.apply_filter(
            args, ["ruff-check"], [pathlib.Path("a.py")], from_run=prefix
        )
    assert exit_early is True
    assert targets is None
    assert "--from-run" in caplog.text
