"""only_failed モジュールのテスト。

``ToolTargets`` dataclass の各分岐と ``apply_filter`` の3状態（fallback / files /
skip→commands絞り込みで除外）を検証する。
``tests/main_test.py`` の ``_apply_only_failed_filter`` 系テストをここへ移管済み。
"""

# pylint: disable=missing-function-docstring

import argparse
import logging
import pathlib

import pytest

import pyfltr.only_failed
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
    """fallback_default() は mode="fallback"、files=() で生成される。"""
    t = pyfltr.only_failed.ToolTargets.fallback_default()
    assert t.mode == "fallback"
    assert len(t.files) == 0


def test_tool_targets_with_files(tmp_path: pathlib.Path) -> None:
    """with_files() は mode="files"、指定ファイルを tuple で保持する。"""
    f = tmp_path / "a.py"
    t = pyfltr.only_failed.ToolTargets.with_files([f])
    assert t.mode == "files"
    assert t.files == (f,)


def test_tool_targets_resolve_files_fallback_returns_all_files(tmp_path: pathlib.Path) -> None:
    """fallback モードは all_files をそのまま返す。"""
    a = tmp_path / "a.py"
    b = tmp_path / "b.py"
    t = pyfltr.only_failed.ToolTargets.fallback_default()
    assert t.resolve_files([a, b]) == [a, b]


def test_tool_targets_resolve_files_files_mode(tmp_path: pathlib.Path) -> None:
    """files モードは self.files のリストを返す。"""
    a = tmp_path / "a.py"
    b = tmp_path / "b.py"
    t = pyfltr.only_failed.ToolTargets.with_files([a])
    # b は含まれない
    assert t.resolve_files([a, b]) == [a]


def test_tool_targets_is_frozen() -> None:
    """frozen=True なのでフィールドへの代入は TypeError になる。"""
    t = pyfltr.only_failed.ToolTargets.fallback_default()
    with pytest.raises((TypeError, AttributeError)):
        t.mode = "files"  # type: ignore[misc]  # ty: ignore[invalid-assignment]


# ---------------------------------------------------------------------------
# apply_filter
# ---------------------------------------------------------------------------


def test_apply_filter_not_only_failed(_only_failed_cache: pathlib.Path) -> None:
    """only_failed=False（未指定）のとき、(commands, None, False) を返す。"""
    args = argparse.Namespace(only_failed=False)
    commands, targets, exit_early = pyfltr.only_failed.apply_filter(args, ["ruff-check"], [])
    assert commands == ["ruff-check"]
    assert targets is None
    assert exit_early is False


def test_apply_filter_builds_per_tool_targets(_only_failed_cache: pathlib.Path) -> None:
    """直前 run の失敗ツールごとに独立した ToolTargets を構築する。"""
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
    commands, targets, exit_early = pyfltr.only_failed.apply_filter(args, ["ruff-check", "mypy"], all_files)

    assert exit_early is False
    assert sorted(commands) == ["mypy", "ruff-check"]
    assert targets is not None
    assert targets["ruff-check"].mode == "files"
    assert targets["ruff-check"].files == (pathlib.Path("a.py"),)
    assert targets["mypy"].mode == "files"
    assert targets["mypy"].files == (pathlib.Path("b.py"),)


def test_apply_filter_includes_resolution_failed_tools(_only_failed_cache: pathlib.Path) -> None:
    """resolution_failed のツールも --only-failed の再実行対象に含まれる。"""
    _seed_run(
        _only_failed_cache,
        commands=["shellcheck"],
        exit_code=1,
        tool_results=[("shellcheck", 1, "ツールが見つかりません", [])],
        resolution_failed_tools={"shellcheck"},
    )
    args = argparse.Namespace(only_failed=True)
    commands, targets, exit_early = pyfltr.only_failed.apply_filter(args, ["shellcheck"], [pathlib.Path("a.sh")])

    assert exit_early is False
    assert commands == ["shellcheck"]
    assert targets is not None
    assert "shellcheck" in targets


def test_apply_filter_fallback_for_missing_diagnostics(_only_failed_cache: pathlib.Path) -> None:
    """診断なしの失敗ツール（pass-filenames=False 等）は fallback モードになる。"""
    _seed_run(
        _only_failed_cache,
        commands=["pytest"],
        exit_code=1,
        tool_results=[("pytest", 1, "test failed", [])],
    )
    args = argparse.Namespace(only_failed=True)
    commands, targets, exit_early = pyfltr.only_failed.apply_filter(args, ["pytest"], [pathlib.Path("tests/t.py")])

    assert exit_early is False
    assert commands == ["pytest"]
    assert targets is not None
    assert targets["pytest"].mode == "fallback"


def test_apply_filter_skip_for_empty_targets_intersection(_only_failed_cache: pathlib.Path) -> None:
    """診断はあるが targets 交差が空のツールは除外され、全ツールで空なら早期終了する。"""
    _seed_run(
        _only_failed_cache,
        commands=["ruff-check"],
        exit_code=1,
        tool_results=[
            ("ruff-check", 1, "", [_make_error("ruff-check", "b.py", 1, "e")]),
        ],
    )
    args = argparse.Namespace(only_failed=True)
    # all_files（targets 由来）に b.py が含まれない → 交差空で早期終了
    _, _, exit_early = pyfltr.only_failed.apply_filter(args, ["ruff-check"], [pathlib.Path("a.py")])
    assert exit_early is True


def test_apply_filter_early_exit_no_runs(_only_failed_cache: pathlib.Path) -> None:
    """直前 run が存在しない場合は exit_early=True（commands は未変更で返す）。"""
    args = argparse.Namespace(only_failed=True)
    commands, targets, exit_early = pyfltr.only_failed.apply_filter(args, ["ruff-check"], [pathlib.Path("a.py")])
    assert exit_early is True
    assert commands == ["ruff-check"]
    assert targets is None


def test_apply_filter_early_exit_no_failures(_only_failed_cache: pathlib.Path) -> None:
    """直前 run が全成功なら exit_early=True（失敗ツール抽出が空）。"""
    _seed_run(
        _only_failed_cache,
        commands=["ruff-check"],
        exit_code=0,
        tool_results=[("ruff-check", 0, "", [])],
    )
    args = argparse.Namespace(only_failed=True)
    _, _, exit_early = pyfltr.only_failed.apply_filter(args, ["ruff-check"], [pathlib.Path("a.py")])
    assert exit_early is True


def test_apply_filter_intersects_with_targets(_only_failed_cache: pathlib.Path) -> None:
    """失敗ファイルと all_files（位置引数 targets 由来）の交差が対象になる。"""
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
    all_files = [pathlib.Path("a.py")]  # targets 指定で b.py は含まない想定
    _, targets, exit_early = pyfltr.only_failed.apply_filter(args, ["ruff-check"], all_files)

    assert exit_early is False
    assert targets is not None
    assert targets["ruff-check"].mode == "files"
    assert targets["ruff-check"].files == (pathlib.Path("a.py"),)


# ---------------------------------------------------------------------------
# apply_filter: --from-run オプション
# ---------------------------------------------------------------------------


def test_apply_filter_from_run_full_id(_only_failed_cache: pathlib.Path) -> None:
    """from_run に完全な run_id を渡すと正しく解決される。"""
    run_id = _seed_run(
        _only_failed_cache,
        commands=["ruff-check"],
        exit_code=1,
        tool_results=[("ruff-check", 1, "", [_make_error("ruff-check", "a.py", 1, "e")])],
    )
    args = argparse.Namespace(only_failed=True)
    _, targets, exit_early = pyfltr.only_failed.apply_filter(args, ["ruff-check"], [pathlib.Path("a.py")], from_run=run_id)
    assert exit_early is False
    assert targets is not None
    assert "ruff-check" in targets


def test_apply_filter_from_run_prefix(_only_failed_cache: pathlib.Path) -> None:
    """from_run に前方一致プレフィックスを渡すと解決される。"""
    run_id = _seed_run(
        _only_failed_cache,
        commands=["ruff-check"],
        exit_code=1,
        tool_results=[("ruff-check", 1, "", [_make_error("ruff-check", "a.py", 1, "e")])],
    )
    prefix = run_id[:8]
    args = argparse.Namespace(only_failed=True)
    _, targets, exit_early = pyfltr.only_failed.apply_filter(args, ["ruff-check"], [pathlib.Path("a.py")], from_run=prefix)
    assert exit_early is False
    assert targets is not None
    assert "ruff-check" in targets


def test_apply_filter_from_run_latest(_only_failed_cache: pathlib.Path) -> None:
    """from_run="latest" エイリアスで最新 run を参照できる。"""
    _seed_run(_only_failed_cache, commands=["ruff-check"], exit_code=0)
    _seed_run(
        _only_failed_cache,
        commands=["ruff-check"],
        exit_code=1,
        tool_results=[("ruff-check", 1, "", [_make_error("ruff-check", "a.py", 1, "e")])],
    )
    args = argparse.Namespace(only_failed=True)
    _, targets, exit_early = pyfltr.only_failed.apply_filter(args, ["ruff-check"], [pathlib.Path("a.py")], from_run="latest")
    assert exit_early is False
    assert targets is not None


def test_apply_filter_from_run_not_found(_only_failed_cache: pathlib.Path, caplog: pytest.LogCaptureFixture) -> None:
    """存在しない run_id を指定すると warning ログを出して早期終了する。"""
    args = argparse.Namespace(only_failed=True)
    with caplog.at_level(logging.WARNING, logger="pyfltr.only_failed"):
        commands, targets, exit_early = pyfltr.only_failed.apply_filter(
            args, ["ruff-check"], [pathlib.Path("a.py")], from_run="nonexistent-run-id"
        )
    assert exit_early is True
    assert targets is None
    assert commands == ["ruff-check"]
    assert "--from-run" in caplog.text


def test_apply_filter_from_run_ambiguous_prefix(_only_failed_cache: pathlib.Path, caplog: pytest.LogCaptureFixture) -> None:
    """曖昧なプレフィックスを指定すると warning を出して早期終了する。"""
    # 2件以上 run を生成して共通プレフィックスを特定する
    run_ids = [_seed_run(_only_failed_cache) for _ in range(3)]
    # 共通プレフィックスを算出する
    shared = 0
    for a, b in zip(run_ids[0], run_ids[1], strict=False):
        if a != b:
            break
        shared += 1
    if shared < 1:
        pytest.skip("共通プレフィックスが無いケースは曖昧判定にならない")
    prefix = run_ids[0][:shared]

    args = argparse.Namespace(only_failed=True)
    with caplog.at_level(logging.WARNING, logger="pyfltr.only_failed"):
        _, targets, exit_early = pyfltr.only_failed.apply_filter(args, ["ruff-check"], [pathlib.Path("a.py")], from_run=prefix)
    assert exit_early is True
    assert targets is None
    assert "--from-run" in caplog.text
