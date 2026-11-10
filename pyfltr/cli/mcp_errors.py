"""MCPの想定内エラーの変換。"""

import types
import typing

try:
    import mcp.server.mcpserver.exceptions as _imported_mcp_exceptions
except ImportError as error:
    _mcp_exceptions: types.ModuleType | None = None
    _MCP_IMPORT_ERROR: ImportError | None = error
else:
    _mcp_exceptions = _imported_mcp_exceptions
    _MCP_IMPORT_ERROR = None


def raise_mcp_error(msg: str) -> typing.Never:
    """MCPクライアントへエラーとして返すための例外を送出する。

    想定内の失敗はmcp SDKの`ToolError`で送出する。
    MCPServerは`ToolError`だけを本文付きの`is_error`応答へ変換し、
    それ以外の例外（`ValueError`など）はクラッシュとして扱い、
    本文を`Error executing tool <ツール名>`へ置き換える（mcp 2.1.0以降）。
    """
    if _mcp_exceptions is None:
        # ツール関数はbuild_server経由で呼ばれ、mcpを読み込めない環境ではbuild_serverが先に停止する。
        raise RuntimeError("MCPサーバー機能に必要な依存を読み込めません") from _MCP_IMPORT_ERROR
    raise _mcp_exceptions.ToolError(msg)
