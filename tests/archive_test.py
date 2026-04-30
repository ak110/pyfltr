"""archive.py のテスト。"""

import datetime
import json
import pathlib

import pytest

import pyfltr.command.core
import pyfltr.command.error_parser
import pyfltr.state.archive
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
    """write_tool_resultでoutput.log / diagnostics.jsonl / tool.jsonが作られる。"""
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

    # diagnostics.jsonlは集約形式で（command、file）単位に1行、messages内に個別指摘
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
    """tool.jsonにhint_urlsが保存され、diagnostics.jsonlのmessagesはruleを保持する。"""
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
    # messages内にrule_urlは入らない
    assert "rule_url" not in entries[0]["messages"][0]

    tool_meta = json.loads((tool_dir / "tool.json").read_text(encoding="utf-8"))
    assert tool_meta["hint_urls"] == {"F401": "https://docs.astral.sh/ruff/rules/F401/"}


def test_write_tool_result_omits_hint_urls_when_no_urls(tmp_path: pathlib.Path) -> None:
    """rule_urlを持たない指摘のみならtool.jsonにhint_urlsキーを出さない。"""
    store = _make_store(tmp_path)
    run_id = store.start_run(commands=["mypy"])
    result = _make_result("mypy", returncode=1, errors=[_make_error("mypy", "a.py", 1, "boom")])
    store.write_tool_result(run_id, result)
    tool_meta = json.loads((tmp_path / "runs" / run_id / "tools" / "mypy" / "tool.json").read_text(encoding="utf-8"))
    assert "hint_urls" not in tool_meta


def test_write_tool_result_stores_hints(tmp_path: pathlib.Path) -> None:
    """hint付きのErrorLocationのとき、tool.jsonにhintsが保存される。"""
    store = _make_store(tmp_path)
    run_id = store.start_run(commands=["textlint"])

    error = _make_error("textlint", "a.md", 1, "長い文", col=1)
    error.rule = "ja-technical-writing/sentence-length"
    error.hint = "句点で文を区切る"
    result = _make_result("textlint", returncode=1, errors=[error])
    store.write_tool_result(run_id, result)

    tool_meta = json.loads((tmp_path / "runs" / run_id / "tools" / "textlint" / "tool.json").read_text(encoding="utf-8"))
    assert tool_meta["hints"] == {"ja-technical-writing/sentence-length": "句点で文を区切る"}


def test_write_tool_result_omits_hints_when_no_hints(tmp_path: pathlib.Path) -> None:
    """hintを持たない指摘のみならtool.jsonにhintsキーを出さない。"""
    store = _make_store(tmp_path)
    run_id = store.start_run(commands=["mypy"])
    result = _make_result("mypy", returncode=1, errors=[_make_error("mypy", "a.py", 1, "boom")])
    store.write_tool_result(run_id, result)
    tool_meta = json.loads((tmp_path / "runs" / run_id / "tools" / "mypy" / "tool.json").read_text(encoding="utf-8"))
    assert "hints" not in tool_meta


def test_finalize_run(tmp_path: pathlib.Path) -> None:
    """finalize_runでmeta.jsonにexit_code / finished_atが追加される。"""
    store = _make_store(tmp_path)
    run_id = store.start_run(commands=["ruff-check"])
    store.finalize_run(run_id, exit_code=1)

    meta = json.loads((tmp_path / "runs" / run_id / "meta.json").read_text(encoding="utf-8"))
    assert meta["exit_code"] == 1
    assert "finished_at" in meta


def test_list_runs_sorted_desc(tmp_path: pathlib.Path) -> None:
    """複数のrunを作成してlist_runsがrun_id降順で返す。"""
    store = _make_store(tmp_path)
    run_ids = [store.start_run() for _ in range(3)]

    summaries = store.list_runs()
    returned_ids = [s.run_id for s in summaries]
    # 降順（新しいものが先頭）
    assert returned_ids == sorted(run_ids, reverse=True)


def test_list_runs_limit(tmp_path: pathlib.Path) -> None:
    """limitで件数制限できる。"""
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
    """存在しないrun_idはFileNotFoundErrorになる。"""
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
    """read_tool_diagnosticsが集約diagnosticレコード一覧を返す。"""
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
    # （mypy、a.py）と（mypy、b.py）の2レコード
    assert len(diagnostics) == 2
    assert diagnostics[0]["file"] == "a.py"
    assert len(diagnostics[0]["messages"]) == 2
    assert diagnostics[1]["file"] == "b.py"
    assert len(diagnostics[1]["messages"]) == 1


def test_list_tools_empty_run(tmp_path: pathlib.Path) -> None:
    """ツール未書き込みのrunではlist_toolsが空リストを返す。"""
    store = _make_store(tmp_path)
    run_id = store.start_run()

    assert store.list_tools(run_id) == []


def test_list_tools_multiple(tmp_path: pathlib.Path) -> None:
    """複数ツール書き込み後のlist_toolsがサニタイズ済み名の自然順リストを返す。"""
    store = _make_store(tmp_path)
    run_id = store.start_run()
    for tool in ("mypy", "ruff-check", "ruff-format"):
        store.write_tool_result(run_id, _make_result(tool, returncode=0))

    assert store.list_tools(run_id) == ["mypy", "ruff-check", "ruff-format"]


def test_list_tools_not_found(tmp_path: pathlib.Path) -> None:
    """存在しないrun_idはFileNotFoundError。"""
    store = _make_store(tmp_path)
    with pytest.raises(FileNotFoundError):
        store.list_tools("nonexistent-run-id")


def test_cleanup_max_runs(tmp_path: pathlib.Path) -> None:
    """101世代作ってmax_runs=100で古いものから1件削除される。"""
    store = _make_store(tmp_path)
    run_ids = [store.start_run() for _ in range(101)]

    policy = pyfltr.state.archive.ArchivePolicy(max_runs=100, max_size_bytes=0, max_age_days=0)
    removed = store.cleanup(policy)

    # 最古の1件が削除される
    assert len(removed) == 1
    assert removed[0] == min(run_ids)
    assert len(store.list_runs()) == 100


def test_cleanup_max_size(tmp_path: pathlib.Path) -> None:
    """大きなoutput.logを持つrunがmax_size超過で削除される。"""
    store = _make_store(tmp_path)
    run_id = store.start_run()
    # 2MBのダミー出力を書き込む
    large_output = "x" * (2 * 1024 * 1024)
    result = _make_result("mypy", returncode=0, output=large_output)
    store.write_tool_result(run_id, result)
    store.finalize_run(run_id, exit_code=0)

    # 2つ目のrun（小さい）
    run_id2 = store.start_run()

    # max_size=1MBでクリーンアップ → 大きい方（古い方）が削除される
    policy = pyfltr.state.archive.ArchivePolicy(max_runs=0, max_size_bytes=1 * 1024 * 1024, max_age_days=0)
    removed = store.cleanup(policy)

    assert run_id in removed
    # 新しいrunは残っている
    remaining_ids = [s.run_id for s in store.list_runs()]
    assert run_id2 in remaining_ids


def test_cleanup_max_age(tmp_path: pathlib.Path) -> None:
    """meta.jsonのstarted_atを過去にしてからmax_age_daysで削除される。"""
    store = _make_store(tmp_path)
    run_id = store.start_run()

    # meta.jsonのstarted_atを60日前に書き換える
    meta_path = tmp_path / "runs" / run_id / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    past = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=60)
    meta["started_at"] = past.isoformat()
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    # 新しいrunを作成
    run_id2 = store.start_run()

    policy = pyfltr.state.archive.ArchivePolicy(max_runs=0, max_size_bytes=0, max_age_days=30)
    removed = store.cleanup(policy)

    # 60日前のrunが削除される
    assert run_id in removed
    remaining_ids = [s.run_id for s in store.list_runs()]
    assert run_id2 in remaining_ids


def test_generate_run_id_unique_and_valid() -> None:
    """連続生成したrun_idは重複なく、ULIDの文字数・文字種を満たす。"""
    ids = [pyfltr.state.archive.generate_run_id() for _ in range(10)]
    # ULIDは26文字のCrockford Base32
    assert all(len(i) == 26 for i in ids)
    assert all(c in "0123456789ABCDEFGHJKMNPQRSTVWXYZ" for i in ids for c in i)
    assert len(set(ids)) == len(ids)


def test_default_cache_root_respects_env(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """PYFLTR_CACHE_DIR環境変数がdefault_cache_rootで優先される。"""
    monkeypatch.setenv("PYFLTR_CACHE_DIR", str(tmp_path))
    result = pyfltr.state.archive.default_cache_root()
    assert result == tmp_path
