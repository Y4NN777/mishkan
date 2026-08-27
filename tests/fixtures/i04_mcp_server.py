"""Deterministic MCP JSON-RPC STDIO fixture for the official client gate."""

from __future__ import annotations

import json
import sys
from typing import Any


def _result(request_id: str | int, result: dict[str, Any]) -> None:
    print(
        json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}),
        flush=True,
    )


for line in sys.stdin:
    request = json.loads(line)
    request_id = request.get("id")
    method = request.get("method")
    if request_id is None:
        continue
    if method == "initialize":
        _result(
            request_id,
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {"tools": {}, "resources": {}, "prompts": {}},
                "serverInfo": {"name": "mishkan-i04-fixture", "version": "1.0"},
            },
        )
    elif method == "tools/list":
        _result(
            request_id,
            {
                "tools": [
                    {
                        "name": "repository.read",
                        "description": "Return deterministic repository evidence.",
                        "inputSchema": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {"path": {"type": "string"}},
                            "required": ["path"],
                        },
                        "outputSchema": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "path": {"type": "string"},
                                "content": {"type": "string"},
                            },
                            "required": ["path", "content"],
                        },
                        "annotations": {"readOnlyHint": True},
                    }
                ]
            },
        )
    elif method == "resources/list":
        _result(
            request_id,
            {
                "resources": [
                    {
                        "name": "fixture.status",
                        "uri": "fixture://status",
                        "mimeType": "application/json",
                    }
                ]
            },
        )
    elif method == "prompts/list":
        _result(
            request_id,
            {
                "prompts": [
                    {
                        "name": "review.evidence",
                        "arguments": [{"name": "subject", "required": True}],
                    }
                ]
            },
        )
    elif method == "tools/call":
        path = request["params"]["arguments"]["path"]
        output = {"path": path, "content": "fixture evidence"}
        _result(
            request_id,
            {
                "content": [{"type": "text", "text": json.dumps(output)}],
                "structuredContent": output,
                "isError": False,
            },
        )
