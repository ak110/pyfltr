import dataclasses
import pathlib

import pyfltr.paths


@dataclasses.dataclass(frozen=True)
class FileUpdate:
    file: pathlib.Path
    before: bytes | None
    after: bytes


class WriteFailure(OSError):
    def __init__(self, cause: OSError, unrestored: list[pathlib.Path]) -> None:
        super().__init__(str(cause))
        self.unrestored = unrestored

    def describe(self) -> str:
        if self.unrestored:
            files = ", ".join(pyfltr.paths.normalize_separators(file) for file in self.unrestored)
            return f"書き込みに失敗し、開始前の内容へ戻せなかったファイルがあります: {files}。原因: {self}"
        return f"書き込みに失敗したため、対象ファイルを開始前の状態へ戻しました。原因: {self}"


def write_updates(updates: list[FileUpdate]) -> None:
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
