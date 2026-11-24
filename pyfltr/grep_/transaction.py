import dataclasses
import pathlib

import pyfltr.paths


@dataclasses.dataclass(frozen=True)
class FileUpdate:
    file: pathlib.Path
    before: bytes | None
    after: bytes


class WriteFailure(OSError):
    def __init__(self, cause: OSError, unrestored: list[pathlib.Path], conflicted: list[pathlib.Path]) -> None:
        super().__init__(str(cause))
        self.unrestored = unrestored
        self.conflicted = conflicted

    def describe(self) -> str:
        parts: list[str] = []
        if self.unrestored:
            parts.append(f"開始前の内容へ戻せなかったファイルがあります: {_join(self.unrestored)}。")
        if self.conflicted:
            parts.append(
                "書き込んだ後に別の編集で変わっていたため、開始前の内容へ戻さず残したファイルがあります: "
                f"{_join(self.conflicted)}。"
                "残した内容を確認してください。"
            )
        if not parts:
            return f"書き込みに失敗したため、対象ファイルを開始前の状態へ戻しました。原因: {self}"
        return f"書き込みに失敗しました。{''.join(parts)}原因: {self}"


def _join(files: list[pathlib.Path]) -> str:
    return ", ".join(pyfltr.paths.normalize_separators(file) for file in files)


def _read_current(file: pathlib.Path) -> bytes | None:
    return file.read_bytes() if file.exists() else None


def write_updates(updates: list[FileUpdate]) -> None:
    written: list[FileUpdate] = []
    interrupted: FileUpdate | None = None
    try:
        for update in updates:
            if _read_current(update.file) != update.before:
                raise OSError(f"読み取った後にファイルが変更されています: {pyfltr.paths.normalize_separators(update.file)}")
            interrupted = update
            update.file.parent.mkdir(parents=True, exist_ok=True)
            update.file.write_bytes(update.after)
            written.append(update)
            interrupted = None
    except OSError as exc:
        unrestored: list[pathlib.Path] = []
        conflicted: list[pathlib.Path] = []
        if interrupted is not None:
            try:
                # 途中まで書いた内容とその後の別の編集は、現在の内容だけでは区別できない。  pyfltr: allow-comment
                if _read_current(interrupted.file) != interrupted.before:
                    unrestored.append(interrupted.file)
            except OSError:
                unrestored.append(interrupted.file)
        for update in reversed(written):
            try:
                current = _read_current(update.file)
                if current == update.before:
                    continue
                if current != update.after:
                    conflicted.append(update.file)
                    continue
                if update.before is None:
                    update.file.unlink(missing_ok=True)
                else:
                    update.file.write_bytes(update.before)
            except OSError:
                unrestored.append(update.file)
        raise WriteFailure(exc, unrestored, conflicted) from exc
