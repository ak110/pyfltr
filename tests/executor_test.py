"""テストコード。"""

import concurrent.futures
import pathlib
import threading
import time

import pyfltr.config
import pyfltr.executor


def test_split_commands_estimated_time_order() -> None:
    """推定実行時間の降順（重いツール先頭）でソートされることのテスト。"""
    config = pyfltr.config.create_default_config()
    # pflake8（コスト0）とmypy/pylint/pytest（コスト非0）はデフォルトで有効
    commands = ["pflake8", "mypy", "pylint", "pytest"]
    # Pythonファイルを含むファイルリストを渡す
    all_files = [pathlib.Path("test.py")]
    _, linters_and_testers = pyfltr.executor.split_commands_for_execution(commands, config, all_files)

    # 推定時間の降順: pytest(3.0), pylint(1.75+0.3), pyright相当なし, mypy(0.2+0.12), pflake8(0.0)
    assert linters_and_testers[0] == "pytest"
    assert linters_and_testers[1] == "pylint"
    assert linters_and_testers[-1] == "pflake8"


def test_split_commands_estimated_time_scales_with_files() -> None:
    """対象ファイル数に応じて推定時間が変化し、ソート順が変わることのテスト。"""
    config = pyfltr.config.create_default_config()
    config.values["textlint"] = True
    # mypy: fixed=0.2, per_file=0.12 → 1ファイルで 0.32
    # textlint: fixed=2.3, per_file=0.4 → mdファイルが対象
    commands = ["mypy", "textlint"]

    # mdファイルなし → textlintの対象は0ファイル（固定コスト2.3のみ）
    # mypy対象は1ファイル（0.2+0.12=0.32）
    all_files = [pathlib.Path("test.py")]
    _, linters = pyfltr.executor.split_commands_for_execution(commands, config, all_files)
    assert linters[0] == "textlint"  # 2.3 > 0.32

    # pyファイル100個 → mypy: 0.2+0.12*100=12.2, textlint: 2.3（mdなし）
    all_files = [pathlib.Path(f"file{i}.py") for i in range(100)]
    _, linters = pyfltr.executor.split_commands_for_execution(commands, config, all_files)
    assert linters[0] == "mypy"  # 12.2 > 2.3


def test_split_commands_fix_mode_puts_all_in_serial_bucket() -> None:
    """fix_mode=True のとき linter も順次実行バケツに積まれる。"""
    config = pyfltr.config.create_default_config()
    config.values["markdownlint"] = True
    commands = ["black", "mypy", "markdownlint"]
    all_files: list[pathlib.Path] = []
    formatters, linters_and_testers = pyfltr.executor.split_commands_for_execution(commands, config, all_files, fix_mode=True)

    # fix モードでは linter/tester バケツは空
    assert not linters_and_testers
    # 全ての enabled コマンドが formatters (= 順次実行) バケツに積まれる
    assert set(formatters) == {"black", "mypy", "markdownlint"}


def test_serial_group_lock_noop_for_none() -> None:
    """serial_group=None は no-op として動作する (他スレッドも即座に進める)。"""
    started = threading.Event()
    finished = threading.Event()

    def _worker() -> None:
        with pyfltr.executor.serial_group_lock(None):
            started.set()
            finished.wait(timeout=1.0)

    thread = threading.Thread(target=_worker)
    thread.start()
    assert started.wait(timeout=1.0), "serial_group=None のロックで待たされてはいけない"
    finished.set()
    thread.join(timeout=1.0)
    assert not thread.is_alive()


def test_serial_group_lock_mutual_exclusion() -> None:
    """同一 serial_group のコマンドは並列実行されても 1 件ずつしか走らない。"""
    # テスト間でグローバル辞書の状態が残らないよう固有のグループ名を使う
    group = "test-mutex"
    concurrent_count = 0
    max_concurrent = 0
    state_lock = threading.Lock()

    def _worker(index: int) -> int:
        nonlocal concurrent_count, max_concurrent
        del index
        with pyfltr.executor.serial_group_lock(group):
            with state_lock:
                concurrent_count += 1
                max_concurrent = max(max_concurrent, concurrent_count)
            # 並列実行が起きていれば max_concurrent が 2 以上になる
            time.sleep(0.05)
            with state_lock:
                concurrent_count -= 1
        return max_concurrent

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(_worker, range(4)))

    # 同一 group なので最大同時実行数は 1 で固定される
    assert max_concurrent == 1, f"serial_group={group!r} で同一グループが並列実行された (max_concurrent={max_concurrent})"


def test_serial_group_lock_independent_groups() -> None:
    """異なる serial_group は互いに独立して並列実行できる。"""
    group_a_running = threading.Event()
    group_b_can_finish = threading.Event()

    def _worker_a() -> None:
        with pyfltr.executor.serial_group_lock("test-group-a"):
            group_a_running.set()
            group_b_can_finish.wait(timeout=1.0)

    def _worker_b() -> None:
        # group_a がロック取得済みの状態で実行されても group_b は即座に進める
        assert group_a_running.wait(timeout=1.0)
        with pyfltr.executor.serial_group_lock("test-group-b"):
            group_b_can_finish.set()

    t_a = threading.Thread(target=_worker_a)
    t_b = threading.Thread(target=_worker_b)
    t_a.start()
    t_b.start()
    t_a.join(timeout=2.0)
    t_b.join(timeout=2.0)
    assert not t_a.is_alive()
    assert not t_b.is_alive()
