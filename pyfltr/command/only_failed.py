"""失敗したツールの再実行対象を表す型。"""

from __future__ import annotations

import dataclasses
import pathlib
import typing

import pyfltr.paths


@dataclasses.dataclass(frozen=True)
class ToolTargets:
    """ツール別の実行対象ファイル指定。

    mode="fallback": 診断ファイルなしのツール（pytest等）でall_filesをそのまま使う。
    mode="files": 失敗ファイルのうち呼び出し時の対象ファイル一覧に含まれるものを対象とする。

    旧形式（`dict[str, list[pathlib.Path] | None]`）では`None`と空リストの
    違いがコードを読むだけでは不明瞭で、フォールバック実行と除外扱いを取り違える
    実装ミスを誘発しやすかった。`mode`属性で二状態を型として区別し、対象ファイル
    リスト取得は`resolve_files()`の単一経路に集約する。
    """

    mode: typing.Literal["fallback", "files"]
    files: tuple[pathlib.Path, ...]

    @classmethod
    def fallback_default(cls) -> ToolTargets:
        """診断ファイルなしのツール向け（all_filesフォールバック）インスタンスを返す。"""
        return cls(mode="fallback", files=())

    @classmethod
    def with_files(cls, files: typing.Iterable[pathlib.Path]) -> ToolTargets:
        """フィルタリング済みファイル集合付きインスタンスを返す。"""
        return cls(mode="files", files=tuple(files))

    def resolve_files(self, all_files: list[pathlib.Path]) -> list[pathlib.Path]:
        """実行対象ファイルのリストを返す。

        mode="fallback"のときall_filesをそのまま返す。
        mode="files"のときself.filesとall_filesの交差をall_filesの順序で返す。

        交差を取るのは、モノレポ分割実行で`ExecutionContext.all_files`が対象のサブプロジェクト
        所属のファイルだけを返すためである。`self.files`は起点cwd全体から抽出した失敗ファイル
        集合であり、そのまま返すと所属しないサブプロジェクトのツールへ起点相対パスが渡り、
        対象不在や解決不能で失敗する。
        """
        if self.mode == "fallback":
            return all_files
        if self.mode == "files":
            selected = {pyfltr.paths.normalize_separators(p) for p in self.files}
            return [p for p in all_files if pyfltr.paths.normalize_separators(p) in selected]
        typing.assert_never(self.mode)
