"""テストコード。"""

import pyfltr.config
import pyfltr.executor


def test_split_commands_fast_order() -> None:
    """fastでないコマンド（重いツール）が先に並ぶことのテスト。"""
    config = pyfltr.config.create_default_config()
    # pflake8とmypy/pylint/pytestはデフォルトで有効
    commands = ["pflake8", "mypy", "pylint", "pytest"]
    _, linters_and_testers = pyfltr.executor.split_commands_for_execution(commands, config)

    # fastでないもの（mypy, pylint, pytest）が先、fastなもの（pflake8）が後
    fast_flags = [config.values.get(f"{c}-fast", False) for c in linters_and_testers]
    assert fast_flags == [False, False, False, True]


def test_split_commands_fix_mode_puts_all_in_serial_bucket() -> None:
    """fix_mode=True のとき linter も順次実行バケツに積まれる。"""
    config = pyfltr.config.create_default_config()
    config.values["markdownlint"] = True
    commands = ["black", "mypy", "markdownlint"]
    formatters, linters_and_testers = pyfltr.executor.split_commands_for_execution(commands, config, fix_mode=True)

    # fix モードでは linter/tester バケツは空
    assert not linters_and_testers
    # 全ての enabled コマンドが formatters (= 順次実行) バケツに積まれる
    assert set(formatters) == {"black", "mypy", "markdownlint"}
