import pathlib

import pytest

import pyfltr.command.execution_slot
import pyfltr.config.model
import pyfltr.grep_.history
import pyfltr.user_cache


def test_user_state_paths_are_isolated_by_default(tmp_path_factory: pytest.TempPathFactory) -> None:
    basetemp = tmp_path_factory.getbasetemp().resolve()
    resolved: dict[str, pathlib.Path] = {
        "cache_root": pyfltr.user_cache.default_cache_root(),
        "execution_slot": pyfltr.command.execution_slot.default_lock_dir(),
        "replace_history": pyfltr.grep_.history.default_history_root(),
        "global_config": pyfltr.config.model.default_global_config_path(),
    }
    outside = {name: path for name, path in resolved.items() if not path.resolve().is_relative_to(basetemp)}
    assert not outside, f"試験用一時領域（{basetemp}）の外を指す保存先がある: {outside}"
