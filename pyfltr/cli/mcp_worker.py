"""MCP要求の同期処理を、要求固有のプロセスで実行する内部入口。"""

import asyncio
import contextlib
import inspect
import json
import logging
import sys

import mcp.server.mcpserver.exceptions
import pydantic

import pyfltr.cli.mcp_server


def main() -> int:
    """stdinの引数を既存ハンドラーへ渡し、stdoutへ結果1件だけを返す。"""
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING, format="%(levelname)s: %(message)s")
    if len(sys.argv) != 2 or sys.argv[1] not in pyfltr.cli.mcp_server.TOOL_HANDLERS:
        sys.stderr.write("MCP workerの処理名が不正です。サーバーから起動してください。\n")
        return 1
    handler = pyfltr.cli.mcp_server.TOOL_HANDLERS[sys.argv[1]]
    # IPCは親がUTF-8のバイト列で送る。Windowsの標準入出力の既定文字コードへ委ねない。
    arguments = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    try:
        # ライブラリ由来のprintもIPCのJSONへ混ぜず、診断用stderrへ届ける。
        with contextlib.redirect_stdout(sys.stderr):
            result = asyncio.run(handler(**arguments))
        adapter = pydantic.TypeAdapter(inspect.signature(handler, eval_str=True).return_annotation)
        response = {"result": adapter.dump_python(result, mode="json")}
    except mcp.server.mcpserver.exceptions.ToolError as exc:
        response = {"error": str(exc)}
    except Exception:  # worker境界の異常はstderrへ残し、内部例外を応答に展開しない。
        logging.exception("MCP workerの処理に失敗しました")
        return 1
    sys.stdout.buffer.write((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
