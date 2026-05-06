"""grep_/history.py のテスト。"""

import datetime
import json
import pathlib
import time

import pytest

import pyfltr.grep_.history
import pyfltr.grep_.replacer
import pyfltr.grep_.types


def _make_command_meta(*, replace_id: str | None) -> pyfltr.grep_.types.ReplaceCommandMeta:
    """テスト用の`ReplaceCommandMeta`を生成する。"""
    return pyfltr.grep_.types.ReplaceCommandMeta(
        replace_id=replace_id,
        dry_run=False,
        fixed_strings=False,
        pattern="foo",
        replacement="bar",
        encoding="utf-8",
    )


def _make_record(file: pathlib.Path) -> pyfltr.grep_.types.ReplaceRecord:
    """テスト用の`ReplaceRecord`を生成する。"""
    return pyfltr.grep_.types.ReplaceRecord(
        file=file,
        line=1,
        col=1,
        before_line="foo line",
        after_line="bar line",
        before_text="foo",
        after_text="bar",
    )


def test_save_and_load_round_trip(tmp_path: pathlib.Path) -> None:
    """save_replace と load_replace の往復で内容が保持される。"""
    store = pyfltr.grep_.history.ReplaceHistoryStore(history_root=tmp_path)
    replace_id = pyfltr.grep_.history.generate_replace_id()
    target = pathlib.Path("src/module.py")
    after_hash = pyfltr.grep_.replacer.compute_hash("after content")

    store.save_replace(
        replace_id,
        command_meta=_make_command_meta(replace_id=replace_id),
        file_changes=[
            {
                "file": target,
                "before_content": "before content",
                "after_hash": after_hash,
                "records": [_make_record(target)],
            }
        ],
    )

    loaded = store.load_replace(replace_id)
    assert loaded["replace_id"] == replace_id
    assert loaded["command"]["pattern"] == "foo"
    assert len(loaded["files"]) == 1
    file_entry = loaded["files"][0]
    assert file_entry["before_content"] == "before content"
    assert file_entry["after_hash"] == after_hash
    assert file_entry["records"][0]["before_text"] == "foo"
    assert file_entry["records"][0]["after_text"] == "bar"


def test_undo_replace_restores_when_hash_matches(tmp_path: pathlib.Path) -> None:
    """ハッシュ一致時にbefore_contentで復元される。"""
    history_root = tmp_path / "history"
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    target = work_dir / "a.txt"
    target.write_text("after content", encoding="utf-8")
    after_hash = pyfltr.grep_.replacer.compute_hash("after content")

    store = pyfltr.grep_.history.ReplaceHistoryStore(history_root=history_root)
    replace_id = pyfltr.grep_.history.generate_replace_id()
    store.save_replace(
        replace_id,
        command_meta=_make_command_meta(replace_id=replace_id),
        file_changes=[
            {
                "file": target,
                "before_content": "before content",
                "after_hash": after_hash,
                "records": [_make_record(target)],
            }
        ],
    )

    restored, skipped = store.undo_replace(replace_id)

    assert restored == [target]
    assert not skipped
    assert target.read_text(encoding="utf-8") == "before content"


def test_undo_replace_skips_when_hash_mismatch(tmp_path: pathlib.Path) -> None:
    """ハッシュ不一致時はforce未指定でスキップされる。"""
    history_root = tmp_path / "history"
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    target = work_dir / "a.txt"
    target.write_text("after content", encoding="utf-8")
    after_hash = pyfltr.grep_.replacer.compute_hash("after content")

    store = pyfltr.grep_.history.ReplaceHistoryStore(history_root=history_root)
    replace_id = pyfltr.grep_.history.generate_replace_id()
    store.save_replace(
        replace_id,
        command_meta=_make_command_meta(replace_id=replace_id),
        file_changes=[
            {
                "file": target,
                "before_content": "before content",
                "after_hash": after_hash,
                "records": [_make_record(target)],
            }
        ],
    )

    # 手動編集を再現
    target.write_text("manually edited content", encoding="utf-8")

    restored, skipped = store.undo_replace(replace_id)

    assert not restored
    assert skipped == [target]
    # 上書きされず手動編集が残る
    assert target.read_text(encoding="utf-8") == "manually edited content"


def test_undo_replace_force_overrides_mismatch(tmp_path: pathlib.Path) -> None:
    """force=True ならハッシュ不一致でも復元する。"""
    history_root = tmp_path / "history"
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    target = work_dir / "a.txt"
    target.write_text("after content", encoding="utf-8")
    after_hash = pyfltr.grep_.replacer.compute_hash("after content")

    store = pyfltr.grep_.history.ReplaceHistoryStore(history_root=history_root)
    replace_id = pyfltr.grep_.history.generate_replace_id()
    store.save_replace(
        replace_id,
        command_meta=_make_command_meta(replace_id=replace_id),
        file_changes=[
            {
                "file": target,
                "before_content": "before content",
                "after_hash": after_hash,
                "records": [_make_record(target)],
            }
        ],
    )

    # 手動編集後にforce=Trueで復元
    target.write_text("manually edited content", encoding="utf-8")
    restored, skipped = store.undo_replace(replace_id, force=True)

    assert restored == [target]
    assert not skipped
    assert target.read_text(encoding="utf-8") == "before content"


def test_load_replace_not_found_raises(tmp_path: pathlib.Path) -> None:
    """存在しない replace_id は FileNotFoundError。"""
    store = pyfltr.grep_.history.ReplaceHistoryStore(history_root=tmp_path)
    with pytest.raises(FileNotFoundError):
        store.load_replace("nonexistent")


def test_list_replaces_returns_descending_order(tmp_path: pathlib.Path) -> None:
    """list_replaces は新しい順で返す。"""
    store = pyfltr.grep_.history.ReplaceHistoryStore(history_root=tmp_path)
    replace_ids: list[str] = []
    for _ in range(3):
        # 同一ミリ秒内でULIDの乱数部分が逆順となる可能性を避ける
        time.sleep(0.001)
        replace_id = pyfltr.grep_.history.generate_replace_id()
        store.save_replace(
            replace_id,
            command_meta=_make_command_meta(replace_id=replace_id),
            file_changes=[],
        )
        replace_ids.append(replace_id)

    listed = store.list_replaces()
    listed_ids = [entry["replace_id"] for entry in listed]
    assert listed_ids == sorted(replace_ids, reverse=True)


def test_list_replaces_limit(tmp_path: pathlib.Path) -> None:
    """list_replaces のlimitで件数を制限できる。"""
    store = pyfltr.grep_.history.ReplaceHistoryStore(history_root=tmp_path)
    for _ in range(5):
        replace_id = pyfltr.grep_.history.generate_replace_id()
        store.save_replace(
            replace_id,
            command_meta=_make_command_meta(replace_id=replace_id),
            file_changes=[],
        )

    listed = store.list_replaces(limit=2)
    assert len(listed) == 2


def test_cleanup_max_entries(tmp_path: pathlib.Path) -> None:
    """世代数超過で古い世代から削除される。"""
    store = pyfltr.grep_.history.ReplaceHistoryStore(history_root=tmp_path)
    replace_ids: list[str] = []
    for _ in range(5):
        time.sleep(0.001)
        replace_id = pyfltr.grep_.history.generate_replace_id()
        store.save_replace(
            replace_id,
            command_meta=_make_command_meta(replace_id=replace_id),
            file_changes=[],
        )
        replace_ids.append(replace_id)

    policy = pyfltr.grep_.history.ReplaceHistoryPolicy(max_entries=3, max_size_bytes=0, max_age_days=0)
    removed = store.cleanup(policy)

    expected_removed = sorted(replace_ids)[:2]
    assert sorted(removed) == sorted(expected_removed)
    remaining = [entry["replace_id"] for entry in store.list_replaces()]
    assert sorted(remaining) == sorted(replace_ids[-3:])


def test_cleanup_max_size(tmp_path: pathlib.Path) -> None:
    """サイズ超過で古い世代から削除される。"""
    store = pyfltr.grep_.history.ReplaceHistoryStore(history_root=tmp_path)
    target = pathlib.Path("a.txt")
    big_payload = "x" * (2 * 1024 * 1024)  # 2MB
    big_id = pyfltr.grep_.history.generate_replace_id()
    after_hash = pyfltr.grep_.replacer.compute_hash(big_payload)
    store.save_replace(
        big_id,
        command_meta=_make_command_meta(replace_id=big_id),
        file_changes=[
            {
                "file": target,
                "before_content": big_payload,
                "after_hash": after_hash,
                "records": [],
            }
        ],
    )
    time.sleep(0.001)
    small_id = pyfltr.grep_.history.generate_replace_id()
    store.save_replace(
        small_id,
        command_meta=_make_command_meta(replace_id=small_id),
        file_changes=[],
    )

    policy = pyfltr.grep_.history.ReplaceHistoryPolicy(max_entries=0, max_size_bytes=1 * 1024 * 1024, max_age_days=0)
    removed = store.cleanup(policy)

    assert big_id in removed
    remaining = [entry["replace_id"] for entry in store.list_replaces()]
    assert small_id in remaining


def test_cleanup_max_age(tmp_path: pathlib.Path) -> None:
    """期間超過で古い世代から削除される。"""
    store = pyfltr.grep_.history.ReplaceHistoryStore(history_root=tmp_path)
    old_id = pyfltr.grep_.history.generate_replace_id()
    store.save_replace(
        old_id,
        command_meta=_make_command_meta(replace_id=old_id),
        file_changes=[],
    )
    # meta.json の saved_at を60日前へ書き換え
    meta_path = tmp_path / old_id / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    past = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=60)
    meta["saved_at"] = past.isoformat()
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    new_id = pyfltr.grep_.history.generate_replace_id()
    store.save_replace(
        new_id,
        command_meta=_make_command_meta(replace_id=new_id),
        file_changes=[],
    )

    policy = pyfltr.grep_.history.ReplaceHistoryPolicy(max_entries=0, max_size_bytes=0, max_age_days=30)
    removed = store.cleanup(policy)

    assert old_id in removed
    remaining = [entry["replace_id"] for entry in store.list_replaces()]
    assert new_id in remaining


def test_default_history_root_respects_cache_env(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """PYFLTR_CACHE_DIR環境変数が default_history_root に反映される。"""
    monkeypatch.setenv("PYFLTR_CACHE_DIR", str(tmp_path))
    result = pyfltr.grep_.history.default_history_root()
    assert result == tmp_path / "replaces"


def test_generate_replace_id_returns_ulid_format() -> None:
    """ULID形式の26文字を返す。"""
    rid = pyfltr.grep_.history.generate_replace_id()
    assert len(rid) == 26
    # Crockford Base32 文字種
    assert all(c in "0123456789ABCDEFGHJKMNPQRSTVWXYZ" for c in rid)
