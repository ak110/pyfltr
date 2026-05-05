"""cache.py のテスト。"""

import os
import pathlib
import time

import pyfltr.command.core_
import pyfltr.config.config
import pyfltr.state.cache
from tests.conftest import make_command_result as _make_result
from tests.conftest import make_error_location as _make_error


def _make_store(tmp_path: pathlib.Path) -> pyfltr.state.cache.CacheStore:
    return pyfltr.state.cache.CacheStore(cache_root=tmp_path)


def _compute_dummy_key(
    store: pyfltr.state.cache.CacheStore,
    command: str,
    target_file: pathlib.Path,
) -> str:
    return store.compute_key(
        command=command,
        commandline=[command, str(target_file)],
        fix_stage=False,
        structured_output=False,
        target_files=[target_file],
        config_files=[],
    )


def test_put_and_get_restores_result(tmp_path: pathlib.Path) -> None:
    """putで保存した結果がgetでcached=True付きで復元される。"""
    store = _make_store(tmp_path)
    target = tmp_path / "foo.md"
    target.write_text("# title\n")

    key = _compute_dummy_key(store, "textlint", target)
    errors = [_make_error("textlint", str(target), 1, "dummy")]
    result = _make_result("textlint", returncode=0, output="ok", errors=errors)
    store.put("textlint", key, result, run_id="01ABCDEFGH")

    restored = store.get("textlint", key)
    assert restored is not None
    assert restored.cached is True
    assert restored.cached_from == "01ABCDEFGH"
    assert restored.returncode == 0
    assert restored.output == "ok"
    assert len(restored.errors) == 1
    assert restored.errors[0].message == "dummy"


def test_get_miss_returns_none(tmp_path: pathlib.Path) -> None:
    """存在しないキーは None を返す。"""
    store = _make_store(tmp_path)
    assert store.get("textlint", "no-such-key") is None


def test_put_without_run_id_is_skipped(tmp_path: pathlib.Path) -> None:
    """run_id=Noneのときは書き込まない（ソース特定不能のため）。"""
    store = _make_store(tmp_path)
    target = tmp_path / "foo.md"
    target.write_text("# title\n")
    key = _compute_dummy_key(store, "textlint", target)
    result = _make_result("textlint", returncode=0, output="ok")
    store.put("textlint", key, result, run_id=None)
    assert store.get("textlint", key) is None


def test_compute_key_changes_on_file_content(tmp_path: pathlib.Path) -> None:
    """対象ファイルの内容が変わるとキーが変わる。"""
    store = _make_store(tmp_path)
    target = tmp_path / "foo.md"
    target.write_text("# one\n")
    key1 = _compute_dummy_key(store, "textlint", target)
    target.write_text("# two\n")
    key2 = _compute_dummy_key(store, "textlint", target)
    assert key1 != key2


def test_compute_key_changes_on_commandline(tmp_path: pathlib.Path) -> None:
    """実効コマンドラインが変わるとキーが変わる（誤ヒット防止）。"""
    store = _make_store(tmp_path)
    target = tmp_path / "foo.md"
    target.write_text("# title\n")
    key_plain = store.compute_key(
        command="textlint",
        commandline=["textlint", str(target)],
        fix_stage=False,
        structured_output=False,
        target_files=[target],
        config_files=[],
    )
    key_with_arg = store.compute_key(
        command="textlint",
        commandline=["textlint", "--extra", str(target)],
        fix_stage=False,
        structured_output=False,
        target_files=[target],
        config_files=[],
    )
    assert key_plain != key_with_arg


def test_compute_key_changes_on_config_file_content(tmp_path: pathlib.Path) -> None:
    """設定ファイルの内容が変わるとキーが変わる。"""
    store = _make_store(tmp_path)
    target = tmp_path / "foo.md"
    target.write_text("# title\n")
    config_file = tmp_path / ".textlintrc"

    config_file.write_text('{"rules": {}}\n')
    key1 = store.compute_key(
        command="textlint",
        commandline=["textlint", str(target)],
        fix_stage=False,
        structured_output=False,
        target_files=[target],
        config_files=[config_file],
    )
    config_file.write_text('{"rules": {"foo": true}}\n')
    key2 = store.compute_key(
        command="textlint",
        commandline=["textlint", str(target)],
        fix_stage=False,
        structured_output=False,
        target_files=[target],
        config_files=[config_file],
    )
    assert key1 != key2


def test_cleanup_removes_old_entries(tmp_path: pathlib.Path) -> None:
    """max_age_hoursを超えたエントリは削除される。"""
    store = _make_store(tmp_path)
    target = tmp_path / "foo.md"
    target.write_text("# title\n")
    key = _compute_dummy_key(store, "textlint", target)
    store.put("textlint", key, _make_result("textlint", returncode=0), run_id="01ABCDEFGH")

    entry_path = store.cache_dir / "textlint" / f"{key}.json"
    assert entry_path.exists()
    # mtimeを2時間前に戻す
    old_mtime = time.time() - 2 * 3600
    os.utime(entry_path, (old_mtime, old_mtime))

    removed = store.cleanup(pyfltr.state.cache.CachePolicy(max_age_hours=1))
    assert len(removed) == 1
    assert not entry_path.exists()


def test_cleanup_keeps_recent_entries(tmp_path: pathlib.Path) -> None:
    """新しいエントリは cleanup で削除されない。"""
    store = _make_store(tmp_path)
    target = tmp_path / "foo.md"
    target.write_text("# title\n")
    key = _compute_dummy_key(store, "textlint", target)
    store.put("textlint", key, _make_result("textlint", returncode=0), run_id="01ABCDEFGH")

    removed = store.cleanup(pyfltr.state.cache.CachePolicy(max_age_hours=1))
    assert not removed


def test_cleanup_zero_policy_is_noop(tmp_path: pathlib.Path) -> None:
    """max_age_hours=0は期間軸クリーンアップ無効。"""
    store = _make_store(tmp_path)
    target = tmp_path / "foo.md"
    target.write_text("# title\n")
    key = _compute_dummy_key(store, "textlint", target)
    store.put("textlint", key, _make_result("textlint", returncode=0), run_id="01ABCDEFGH")
    entry_path = store.cache_dir / "textlint" / f"{key}.json"
    os.utime(entry_path, (0, 0))  # 1970年扱い

    removed = store.cleanup(pyfltr.state.cache.CachePolicy(max_age_hours=0))
    assert not removed
    assert entry_path.exists()


def test_is_cacheable_true_for_textlint() -> None:
    """textlint は cacheable=True。"""
    config = pyfltr.config.config.create_default_config()
    assert pyfltr.state.cache.is_cacheable("textlint", config, additional_args=[])


def test_is_cacheable_false_for_mypy() -> None:
    """cacheable=Falseのツール（mypyなど）は対象外。"""
    config = pyfltr.config.config.create_default_config()
    assert not pyfltr.state.cache.is_cacheable("mypy", config, additional_args=[])


def test_is_cacheable_false_with_config_arg() -> None:
    """`--{command}-args`に`--config`を含む場合は対象外。"""
    config = pyfltr.config.config.create_default_config()
    assert not pyfltr.state.cache.is_cacheable("textlint", config, additional_args=["--config", "/tmp/t.json"])
    assert not pyfltr.state.cache.is_cacheable("textlint", config, additional_args=["--config=/tmp/t.json"])


def test_is_cacheable_false_with_ignore_path_arg() -> None:
    """`--{command}-args`に`--ignore-path`を含む場合は対象外。"""
    config = pyfltr.config.config.create_default_config()
    assert not pyfltr.state.cache.is_cacheable("textlint", config, additional_args=["--ignore-path", "/tmp/i"])
    assert not pyfltr.state.cache.is_cacheable("textlint", config, additional_args=["--ignore-path=/tmp/i"])


def test_resolve_config_files_textlint() -> None:
    """textlintのconfig_filesが完全列挙される。"""
    config = pyfltr.config.config.create_default_config()
    files = pyfltr.state.cache.resolve_config_files("textlint", config, base=pathlib.Path("/tmp"))
    assert pathlib.Path("/tmp/.textlintrc") in files
    assert pathlib.Path("/tmp/.textlintignore") in files
    assert pathlib.Path("/tmp/package.json") in files


def test_cache_policy_from_config_uses_default() -> None:
    """既定値は12時間。"""
    config = pyfltr.config.config.create_default_config()
    policy = pyfltr.state.cache.cache_policy_from_config(config)
    assert policy.max_age_hours == 12


def test_cache_policy_respects_override() -> None:
    """設定値で上書きできる。"""
    config = pyfltr.config.config.create_default_config()
    config.values["cache-max-age-hours"] = 24
    policy = pyfltr.state.cache.cache_policy_from_config(config)
    assert policy.max_age_hours == 24
