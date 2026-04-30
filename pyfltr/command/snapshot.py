"""ファイル変更検知。"""

import hashlib
import pathlib

import pyfltr.paths
import pyfltr.warnings_

logger = __import__("logging").getLogger(__name__)


def _snapshot_file_digests(targets: list[pathlib.Path]) -> dict[pathlib.Path, bytes]:
    """対象ファイルの内容ハッシュ （BLAKE2b） スナップショットを取得。

    mtimeベースの比較はtextlint --fixのように「残存違反がなくても
    ファイルを書き戻す」ツールで偽陽性を起こすため、内容ハッシュで比較する。
    ファイルが存在しない場合は空bytesを設定する （比較で差分検知できる）。
    """
    result: dict[pathlib.Path, bytes] = {}
    for target in targets:
        try:
            with target.open("rb") as f:
                result[target] = hashlib.file_digest(f, "blake2b").digest()
        except OSError:
            result[target] = b""
    return result


def _changed_files(
    before: dict[pathlib.Path, bytes],
    after: dict[pathlib.Path, bytes],
) -> list[str]:
    """ハッシュスナップショット前後で内容が変化したファイルのパス文字列リストを返す。

    `_snapshot_file_digests` の戻り値を2点渡し、ハッシュが変化したキーを抽出する。
    結果は文字列化してソートして返す（summary.applied_fixesの安定ソート用）。
    """
    return sorted(str(p) for p, digest in after.items() if before.get(p) != digest)


def _snapshot_file_texts(targets: list[pathlib.Path]) -> dict[pathlib.Path, str]:
    """対象ファイルのテキスト内容スナップショットを取得する。

    textlint fixの保護対象識別子破損検知に使う。読み込めないファイルは辞書から
    除外する （比較時には「前後どちらにも出現しない」と解釈される）。
    """
    result: dict[pathlib.Path, str] = {}
    for target in targets:
        try:
            result[target] = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
    return result


def _warn_protected_identifier_corruption(
    before: dict[pathlib.Path, str],
    after: dict[pathlib.Path, str],
    protected_identifiers: list[str],
) -> None:
    """Textlint fix後に保護対象識別子が失われていた場合、警告を発行する。

    fix前のファイル内容に含まれていた識別子がfix後に1件でも減っていれば、
    当該識別子が `preset-jtf-style` などの機械変換で破損した可能性が高い。
    検知は出現回数ベース （等号比較） で行い、単純な減少も破損として扱う。
    """
    for path, before_text in before.items():
        after_text = after.get(path)
        if after_text is None:
            continue
        if before_text == after_text:
            continue  # 変化なしの場合は検査不要
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
