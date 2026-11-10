"""JSONLのheartbeat監視。"""

import threading
import time
import typing

import pyfltr.output.jsonl
import pyfltr.output.jsonl_emit
import pyfltr.output.logging_

text_logger = pyfltr.output.logging_.text_logger
lock = pyfltr.output.logging_.text_output_lock

# heartbeat監視の発火しきい値（秒）。
# パイプライン全体の「最後のJSONL出力からの経過時間」がこの値を超えると、
# 実行中コマンドそれぞれに対して `status:"running"` レコードを発行する。
# 設定キー化は複雑度を増やすだけで利用頻度が低いため固定値とする。
_HEARTBEAT_THRESHOLD_SECONDS: float = 30.0
"""heartbeat発火しきい値（最後のJSONL出力からの無音時間、秒）。"""


_HEARTBEAT_TICK_INTERVAL: float = 5.0
"""heartbeat監視ループの判定間隔（秒）。

しきい値より細かく刻むことで、しきい値超過の検知遅れを最大 `_HEARTBEAT_TICK_INTERVAL` 秒に抑える。
発火自体は最終出力からの経過時間で判定するため、本値は「監視解像度」の調整値。
"""


class HeartbeatMonitor:
    """パイプライン全体のheartbeat監視。

    別スレッドで一定間隔ループし、`pyfltr.output.jsonl_emit.get_last_jsonl_output_time()` から
    最終JSONL出力時刻を取得して経過時間を判定する。
    発火条件は「最後のJSONL出力から既定 `_HEARTBEAT_THRESHOLD_SECONDS` 秒経過した場合」のみ。
    subprocess側のstdout出力の有無は判定材料に含めない（LLMから観測できるのはpyfltr側のJSONL
    出力のみで、子プロセスの静粛さは観測対象外のため）。
    しきい値超過時は実行中コマンド集合（`{command_name -> start_time}`）から各commandへ
    `status:"running"` レコードを発行する。複数コマンドが同時にハングしている場合も
    各コマンドの状態を識別できるよう、実行中の全コマンドそれぞれに発行する設計。
    完了時の最終レコード（`status:"failed"` / `"succeeded"` 等）が必ず後続することを保証する。
    text_loggerにも発火を残し、人間向け表示でも進捗が確認できるようにする。

    `start()` / `stop()` を `run_pipeline` の入口・出口で呼ぶ。
    `on_command_start(command)` / `on_command_end(command)` は subprocess の開始・終了に
    フックして登録・削除する。並列実行下のアクセスは `_lock` で保護する。
    """

    def __init__(
        self,
        *,
        threshold: float = _HEARTBEAT_THRESHOLD_SECONDS,
        tick_interval: float = _HEARTBEAT_TICK_INTERVAL,
        emit_running: typing.Callable[[str, float], None] | None = None,
        emit_text: typing.Callable[[str], None] | None = None,
        get_last_output_time: typing.Callable[[], float | None] | None = None,
        set_last_output_time: typing.Callable[[float], None] | None = None,
    ) -> None:
        self._threshold = threshold
        self._tick_interval = tick_interval
        self._emit_running = emit_running or pyfltr.output.jsonl.write_jsonl_running_event
        self._emit_text = emit_text or _heartbeat_text_emit
        self._get_last_output_time = get_last_output_time or pyfltr.output.jsonl_emit.get_last_jsonl_output_time
        self._set_last_output_time = set_last_output_time or pyfltr.output.jsonl_emit.set_last_jsonl_output_time
        self._lock = threading.Lock()
        self._running: dict[str, float] = {}
        self._finished: set[str] = set()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """監視スレッドを起動する。`stop()` 呼び出しまで非daemon相当でループする。

        起動時点を初期最終出力時刻として常時上書きする。
        モジュールグローバルな`_last_jsonl_output_time`は同一プロセスで`run_pipeline`が
        複数回呼ばれるMCP経路で前回値が残存し得るため、毎回起動時に明示的に上書きする
        （未初期化判定では2回目以降の起動で前回値がそのまま使われ、
        起動直後にしきい値超過とみなされる誤発火を招く）。
        """
        if self._thread is not None:
            return
        self._set_last_output_time(time.monotonic())
        self._stop_event.clear()
        thread = threading.Thread(target=self._loop, name="pyfltr-heartbeat", daemon=True)
        self._thread = thread
        thread.start()

    def stop(self) -> None:
        """監視スレッドを停止する。"""
        if self._thread is None:
            return
        self._stop_event.set()
        self._thread.join()
        self._thread = None

    def on_command_start(self, command: str) -> None:
        """実行中コマンド集合に追加する。"""
        with self._lock:
            if command not in self._finished:
                self._running[command] = time.monotonic()

    def on_command_end(self, command: str) -> None:
        """実行中コマンド集合から削除する。"""
        with self._lock:
            self._running.pop(command, None)

    def finalize(self, command: str, emit: typing.Callable[[], None]) -> None:
        """最終結果の確定と発行をheartbeatと同じ排他境界で行う。

        subprocessの終了は段間にも発生するため、最終結果だけをここで確定する。
        遅延した開始通知も確定済みcommandを実行中へ戻さない。
        """
        with self._lock:
            self._finished.add(command)
            self._running.pop(command, None)
            emit()

    def _snapshot_running(self) -> list[tuple[str, float]]:
        """現時点の実行中コマンドのリスト（command, start_time）を返す。"""
        with self._lock:
            return list(self._running.items())

    def _loop(self) -> None:
        """監視ループ本体。`_tick_interval` 毎にしきい値判定を行う。"""
        while not self._stop_event.wait(self._tick_interval):
            self.tick(time.monotonic())

    def tick(self, now: float) -> None:
        """1回分の判定。しきい値超過なら実行中各コマンドへrunningイベントを発行する。

        テストから時刻依存の発火検証を行うためpublic APIとして公開する。
        通常運用では`_loop`から自動的に呼ばれる。
        """
        last_output = self._get_last_output_time()
        if last_output is None:
            return
        silence = now - last_output
        if silence <= self._threshold:
            return
        running = self._snapshot_running()
        if not running:
            # 実行中コマンドが無ければheartbeat発行しても意味が無いため抑止する。
            # ただし無音時間カウンタは更新して連続ティックでの再判定を抑える。
            self._set_last_output_time(now)
            return
        for command, started in running:
            with self._lock:
                if self._stop_event.is_set() or command in self._finished or self._running.get(command) != started:
                    continue
                elapsed = now - started
                self._emit_text(f"{command} running for {elapsed:.0f}s (no JSONL output for {silence:.0f}s)")
                self._emit_running(command, elapsed)
        # heartbeat発火後、`_emit_running`内の `pyfltr.output.jsonl_emit._emit_structured` で `last_jsonl_output_time` が
        # 自動的に更新されるため、明示更新は不要。次の判定はそこから再びしきい値経過後に発火する。


def _heartbeat_text_emit(message: str) -> None:
    """heartbeat発火時のtext_logger経由warning出力。

    text_loggerはoutput_formatごとに出力先（stdout/stderr）が切り替わるため、
    本関数はその差分を吸収する単純なラッパー。
    """
    with lock:
        text_logger.warning(message)
