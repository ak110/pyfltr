"""archive.py のテスト。"""

import datetime
import json
import pathlib

import pytest

import pyfltr.archive
import pyfltr.command
import pyfltr.error_parser
from tests.conftest import make_archive_store as _make_store
from tests.conftest import make_command_result as _make_result
from tests.conftest import make_error_location as _make_error


def test_start_run_creates_directory_and_meta(tmp_path: pathlib.Path) -> None:
    """start_run で runs/<run_id>/meta.json が作られ、必要なキーが含まれる。"""
    store = _make_store(tmp_path)
    run_id = store.start_run(commands=["mypy", "ruff-check"], files=10)

    meta_path = tmp_path / "runs" / run_id / "meta.json"
    assert meta_path.exists()

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["run_id"] == run_id
    assert "version" in meta
    assert "python" in meta
    assert "started_at" in meta
    assert meta["commands"] == ["mypy", "ruff-check"]
    assert meta["files"] == 10


def test_write_tool_result(tmp_path: pathlib.Path) -> None:
    """write_tool_result で output.log / diagnostics.jsonl / tool.json が作られる。"""
    store = _make_store(tmp_path)
    run_id = store.start_run(commands=["mypy"])

    errors = [_make_error("mypy", "src/a.py", 10, "型エラー", col=5)]
    result = _make_result("mypy", returncode=1, output="mypy output", errors=errors)
    store.write_tool_result(run_id, result)

    tool_dir = tmp_path / "runs" / run_id / "tools" / "mypy"
    assert (tool_dir / "output.log").exists()
    assert (tool_dir / "tool.json").exists()
    assert (tool_dir / "diagnostics.jsonl").exists()

    # output.log の内容確認
    assert (tool_dir / "output.log").read_text(encoding="utf-8") == "mypy output"

    # diagnostics.jsonl は集約形式で (command, file) 単位に 1 行、messages 内に個別指摘
    lines = [json.loads(line) for line in (tool_dir / "diagnostics.jsonl").read_text(encoding="utf-8").splitlines() if line]
    assert len(lines) == 1
    assert lines[0]["kind"] == "diagnostic"
    assert lines[0]["command"] == "mypy"
    assert lines[0]["file"] == "src/a.py"
    messages = lines[0]["messages"]
    assert len(messages) == 1
    assert messages[0]["line"] == 10
    assert messages[0]["col"] == 5
    assert messages[0]["msg"] == "型エラー"


def test_write_tool_result_stores_hint_urls(tmp_path: pathlib.Path) -> None:
    """tool.json に hint-urls が保存され、diagnostics.jsonl の messages は rule を保持する。"""
    store = _make_store(tmp_path)
    run_id = store.start_run(commands=["ruff-check"])

    error = _make_error("ruff-check", "src/foo.py", 10, "unused import", col=5)
    error.rule = "F401"
    error.rule_url = "https://docs.astral.sh/ruff/rules/F401/"
    error.severity = "error"
    result = _make_result("ruff-check", returncode=1, output="ruff output", errors=[error])
    store.write_tool_result(run_id, result)

    tool_dir = tmp_path / "runs" / run_id / "tools" / "ruff-check"
    diagnostics_path = tool_dir / "diagnostics.jsonl"
    entries = [json.loads(line) for line in diagnostics_path.read_text(encoding="utf-8").splitlines() if line]
    assert entries[0]["file"] == "src/foo.py"
    assert entries[0]["messages"][0]["rule"] == "F401"
    # messages 内に rule_url は入らない
    assert "rule_url" not in entries[0]["messages"][0]

    tool_meta = json.loads((tool_dir / "tool.json").read_text(encoding="utf-8"))
    assert tool_meta["hint-urls"] == {"F401": "https://docs.astral.sh/ruff/rules/F401/"}


def test_write_tool_result_omits_hint_urls_when_no_urls(tmp_path: pathlib.Path) -> None:
    """rule_url を持たない指摘のみなら tool.json に hint-urls キーを出さない。"""
    store = _make_store(tmp_path)
    run_id = store.start_run(commands=["mypy"])
    result = _make_result("mypy", returncode=1, errors=[_make_error("mypy", "a.py", 1, "boom")])
    store.write_tool_result(run_id, result)
    tool_meta = json.loads((tmp_path / "runs" / run_id / "tools" / "mypy" / "tool.json").read_text(encoding="utf-8"))
    assert "hint-urls" not in tool_meta


def test_finalize_run(tmp_path: pathlib.Path) -> None:
    """finalize_run で meta.json に exit_code / finished_at が追加される。"""
    store = _make_store(tmp_path)
    run_id = store.start_run(commands=["ruff-check"])
    store.finalize_run(run_id, exit_code=1)

    meta = json.loads((tmp_path / "runs" / run_id / "meta.json").read_text(encoding="utf-8"))
    assert meta["exit_code"] == 1
    assert "finished_at" in meta


def test_list_runs_sorted_desc(tmp_path: pathlib.Path) -> None:
    """複数の run を作成して list_runs が run_id 降順で返す。"""
    store = _make_store(tmp_path)
    run_ids = [store.start_run() for _ in range(3)]

    summaries = store.list_runs()
    returned_ids = [s.run_id for s in summaries]
    # 降順（新しいものが先頭）
    assert returned_ids == sorted(run_ids, reverse=True)


def test_list_runs_limit(tmp_path: pathlib.Path) -> None:
    """limit で件数制限できる。"""
    store = _make_store(tmp_path)
    for _ in range(5):
        store.start_run()

    summaries = store.list_runs(limit=3)
    assert len(summaries) == 3


def test_read_meta(tmp_path: pathlib.Path) -> None:
    """read_meta が meta.json を正しく返す。"""
    store = _make_store(tmp_path)
    run_id = store.start_run(commands=["mypy"], files=5)

    meta = store.read_meta(run_id)
    assert meta["run_id"] == run_id
    assert meta["commands"] == ["mypy"]
    assert meta["files"] == 5


def test_read_meta_not_found(tmp_path: pathlib.Path) -> None:
    """存在しない run_id は FileNotFoundError になる。"""
    store = _make_store(tmp_path)
    with pytest.raises(FileNotFoundError):
        store.read_meta("nonexistent-run-id")


def test_read_tool_meta(tmp_path: pathlib.Path) -> None:
    """read_tool_meta がツールのメタ情報を返す。"""
    store = _make_store(tmp_path)
    run_id = store.start_run()
    result = _make_result("ruff-check", returncode=0)
    store.write_tool_result(run_id, result)

    tool_meta = store.read_tool_meta(run_id, "ruff-check")
    assert tool_meta["command"] == "ruff-check"
    assert tool_meta["returncode"] == 0


def test_read_tool_output(tmp_path: pathlib.Path) -> None:
    """read_tool_output が生出力を返す。"""
    store = _make_store(tmp_path)
    run_id = store.start_run()
    result = _make_result("ruff-check", returncode=0, output="all clear")
    store.write_tool_result(run_id, result)

    output = store.read_tool_output(run_id, "ruff-check")
    assert output == "all clear"


def test_read_tool_diagnostics(tmp_path: pathlib.Path) -> None:
    """read_tool_diagnostics が集約 diagnostic レコード一覧を返す。"""
    store = _make_store(tmp_path)
    run_id = store.start_run()
    errors = [
        _make_error("mypy", "a.py", 1, "error A"),
        _make_error("mypy", "a.py", 3, "error A2"),
        _make_error("mypy", "b.py", 2, "error B"),
    ]
    result = _make_result("mypy", returncode=1, errors=errors)
    store.write_tool_result(run_id, result)

    diagnostics = store.read_tool_diagnostics(run_id, "mypy")
    # (mypy, a.py) と (mypy, b.py) の 2 レコード
    assert len(diagnostics) == 2
    assert diagnostics[0]["file"] == "a.py"
    assert len(diagnostics[0]["messages"]) == 2
    assert diagnostics[1]["file"] == "b.py"
    assert len(diagnostics[1]["messages"]) == 1


def test_list_tools_empty_run(tmp_path: pathlib.Path) -> None:
    """ツール未書き込みの run では list_tools が空リストを返す。"""
    store = _make_store(tmp_path)
    run_id = store.start_run()

    assert store.list_tools(run_id) == []


def test_list_tools_multiple(tmp_path: pathlib.Path) -> None:
    """複数ツール書き込み後の list_tools がサニタイズ済み名の自然順リストを返す。"""
    store = _make_store(tmp_path)
    run_id = store.start_run()
    for tool in ("mypy", "ruff-check", "ruff-format"):
        store.write_tool_result(run_id, _make_result(tool, returncode=0))

    assert store.list_tools(run_id) == ["mypy", "ruff-check", "ruff-format"]


def test_list_tools_not_found(tmp_path: pathlib.Path) -> None:
    """存在しない run_id は FileNotFoundError。"""
    store = _make_store(tmp_path)
    with pytest.raises(FileNotFoundError):
        store.list_tools("nonexistent-run-id")


def test_cleanup_max_runs(tmp_path: pathlib.Path) -> None:
    """101 世代作って max_runs=100 で古いものから 1 件削除される。"""
    store = _make_store(tmp_path)
    run_ids = [store.start_run() for _ in range(101)]

    policy = pyfltr.archive.ArchivePolicy(max_runs=100, max_size_bytes=0, max_age_days=0)
    removed = store.cleanup(policy)

    # 最古の 1 件が削除される
    assert len(removed) == 1
    assert removed[0] == min(run_ids)
    assert len(store.list_runs()) == 100


def test_cleanup_max_size(tmp_path: pathlib.Path) -> None:
    """大きな output.log を持つ run が max_size 超過で削除される。"""
    store = _make_store(tmp_path)
    run_id = store.start_run()
    # 2MB のダミー出力を書き込む
    large_output = "x" * (2 * 1024 * 1024)
    result = _make_result("mypy", returncode=0, output=large_output)
    store.write_tool_result(run_id, result)
    store.finalize_run(run_id, exit_code=0)

    # 2 つ目の run（小さい）
    run_id2 = store.start_run()

    # max_size=1MB でクリーンアップ → 大きい方（古い方）が削除される
    policy = pyfltr.archive.ArchivePolicy(max_runs=0, max_size_bytes=1 * 1024 * 1024, max_age_days=0)
    removed = store.cleanup(policy)

    assert run_id in removed
    # 新しい run は残っている
    remaining_ids = [s.run_id for s in store.list_runs()]
    assert run_id2 in remaining_ids


def test_cleanup_max_age(tmp_path: pathlib.Path) -> None:
    """meta.json の started_at を過去にしてから max_age_days で削除される。"""
    store = _make_store(tmp_path)
    run_id = store.start_run()

    # meta.json の started_at を 60 日前に書き換える
    meta_path = tmp_path / "runs" / run_id / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    past = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=60)
    meta["started_at"] = past.isoformat()
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    # 新しい run を作成
    run_id2 = store.start_run()

    policy = pyfltr.archive.ArchivePolicy(max_runs=0, max_size_bytes=0, max_age_days=30)
    removed = store.cleanup(policy)

    # 60 日前の run が削除される
    assert run_id in removed
    remaining_ids = [s.run_id for s in store.list_runs()]
    assert run_id2 in remaining_ids


def test_generate_run_id_monotonic_increasing() -> None:
    """連続生成した run_id は辞書順ソートで時系列を保つ（ULID の特性）。"""
    ids = [pyfltr.archive.generate_run_id() for _ in range(10)]
    # 生成順と辞書順が一致する（ULID はタイムスタンプ埋め込みで辞書順 = 時系列順）
    assert ids == sorted(ids)


def test_default_cache_root_respects_env(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """PYFLTR_CACHE_DIR 環境変数が default_cache_root で優先される。"""
    monkeypatch.setenv("PYFLTR_CACHE_DIR", str(tmp_path))
    result = pyfltr.archive.default_cache_root()
    assert result == tmp_path
