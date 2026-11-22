import hashlib
import pathlib

import pyfltr.paths
import pyfltr.warnings_

logger = __import__("logging").getLogger(__name__)


def snapshot_file_digests(targets: list[pathlib.Path], *, base_cwd: pathlib.Path | None = None) -> dict[pathlib.Path, bytes]:
    result: dict[pathlib.Path, bytes] = {}
    for target in targets:
        path = target if target.is_absolute() or base_cwd is None else base_cwd / target
        try:
            with path.open("rb") as f:
                result[target] = hashlib.file_digest(f, "blake2b").digest()
        except OSError:
            result[target] = b""
    return result


def changed_files(
    before: dict[pathlib.Path, bytes],
    after: dict[pathlib.Path, bytes],
) -> list[str]:
    return sorted(pyfltr.paths.normalize_separators(p) for p, digest in after.items() if before.get(p) != digest)


def snapshot_file_texts(targets: list[pathlib.Path], *, base_cwd: pathlib.Path | None = None) -> dict[pathlib.Path, str]:
    result: dict[pathlib.Path, str] = {}
    for target in targets:
        path = target if target.is_absolute() or base_cwd is None else base_cwd / target
        try:
            result[target] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
    return result


def warn_protected_identifier_corruption(
    before: dict[pathlib.Path, str],
    after: dict[pathlib.Path, str],
    protected_identifiers: list[str],
) -> None:
    for path, before_text in before.items():
        after_text = after.get(path)
        if after_text is None:
            continue
        if before_text == after_text:
            continue
        for identifier in protected_identifiers:
            before_count = before_text.count(identifier)
            after_count = after_text.count(identifier)
            if before_count > after_count:
                pyfltr.warnings_.emit_warning(
                    source="textlint-identifier-corruption",
                    message=(
                        f"textlint fix が保護対象識別子を変換した可能性: "
                        f"{identifier!r} (file={pyfltr.paths.to_cwd_relative(path)}, "
                        f"before={before_count}, after={after_count})"
                    ),
                    hint="保護したい識別子はバックティックで囲むとtextlintのfixで改変されなくなる",
                )
