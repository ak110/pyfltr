"""複数ファイルの書き込みと、I/O失敗時の開始前状態への復旧。"""

import dataclasses
import pathlib

import pyfltr.paths


@dataclasses.dataclass(frozen=True)
class FileUpdate:
    """開始前の状態と書き込み予定を保持する。Noneは元ファイルの不在を表す。"""

    file: pathlib.Path
    before: bytes | None
    after: bytes


class WriteFailure(OSError):
    """書き込み失敗と、開始前状態へ戻せなかった対象を保持する。"""

    def __init__(self, cause: OSError, unrestored: list[pathlib.Path]) -> None:
        super().__init__(str(cause))
        self.unrestored = unrestored

    def describe(self) -> str:
        """元の原因と復旧結果を返す。"""
        if self.unrestored:
            files = ", ".join(pyfltr.paths.normalize_separators(file) for file in self.unrestored)
            return f"書き込みに失敗し、開始前の内容へ戻せなかったファイルがあります: {files}。原因: {self}"
        return f"書き込みに失敗したため、対象ファイルを開始前の状態へ戻しました。原因: {self}"


def write_updates(updates: list[FileUpdate]) -> None:
    """全対象を更新し、途中失敗時は書き込みを試みた対象自身も復旧する。

    ファイルのinode・権限・リンク関係を維持するため通常の書き込みを使う。
    部分書き込みを起こした対象も復旧し、1件の復旧失敗で他の復旧を打ち切らない。
    強制終了や電源断で全ファイルを原子的に変更する保証は持たない。
    """
    attempted: list[FileUpdate] = []
    try:
        for update in updates:
            current = update.file.read_bytes() if update.file.exists() else None
            if current != update.before:
                raise OSError(f"置換の準備後にファイルが変更されています: {pyfltr.paths.normalize_separators(update.file)}")
            attempted.append(update)
            update.file.parent.mkdir(parents=True, exist_ok=True)
            update.file.write_bytes(update.after)
    except OSError as exc:
        unrestored: list[pathlib.Path] = []
        for update in reversed(attempted):
            try:
                if update.before is None:
                    update.file.unlink(missing_ok=True)
                else:
                    update.file.write_bytes(update.before)
            except OSError:
                unrestored.append(update.file)
        raise WriteFailure(exc, unrestored) from exc
