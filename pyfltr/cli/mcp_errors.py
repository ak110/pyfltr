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
    if _mcp_exceptions is None:
        raise RuntimeError("MCPサーバー機能に必要な依存を読み込めません") from _MCP_IMPORT_ERROR
    raise _mcp_exceptions.ToolError(msg)
