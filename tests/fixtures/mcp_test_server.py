"""Deterministic MCP JSON-RPC STDIO fixture for the official client gate."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4


def _task_state_path() -> Path | None:
    if len(sys.argv) == 3 and sys.argv[1] == "--task-state":
        return Path(sys.argv[2])
    return None


TASK_STATE = _task_state_path()


def _load_tasks() -> dict[str, dict[str, Any]]:
    if TASK_STATE is None or not TASK_STATE.exists():
        return {}
    value = json.loads(TASK_STATE.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _save_tasks(tasks: dict[str, dict[str, Any]]) -> None:
    assert TASK_STATE is not None
    TASK_STATE.parent.mkdir(parents=True, exist_ok=True)
    temporary = TASK_STATE.with_suffix(".tmp")
    temporary.write_text(json.dumps(tasks, sort_keys=True), encoding="utf-8")
    temporary.replace(TASK_STATE)


def _task_view(task_id: str, task: dict[str, Any]) -> dict[str, Any]:
    return {
        "taskId": task_id,
        "status": task["status"],
        "statusMessage": task["status_message"],
        "createdAt": task["created_at"],
        "lastUpdatedAt": task["updated_at"],
        "ttl": task["ttl"],
        "pollInterval": 10,
    }


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
        capabilities: dict[str, Any] = {"tools": {}, "resources": {}, "prompts": {}}
        if TASK_STATE is not None:
            capabilities["tasks"] = {
                "list": {},
                "cancel": {},
                "requests": {"tools": {"call": {}}},
            }
        _result(
            request_id,
            {
                "protocolVersion": "2025-11-25",
                "capabilities": capabilities,
                "serverInfo": {"name": "mishkan-mcp-fixture", "version": "1.0"},
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
        if request["params"].get("task") is not None:
            now = datetime.now(UTC).isoformat()
            task_id = str(uuid4())
            tasks = _load_tasks()
            tasks[task_id] = {
                "status": "working",
                "status_message": "fixture task accepted",
                "created_at": now,
                "updated_at": now,
                "ttl": request["params"]["task"].get("ttl"),
                "path": path,
            }
            _save_tasks(tasks)
            _result(request_id, {"task": _task_view(task_id, tasks[task_id])})
            continue
        output = {"path": path, "content": "fixture evidence"}
        _result(
            request_id,
            {
                "content": [{"type": "text", "text": json.dumps(output)}],
                "structuredContent": output,
                "isError": False,
            },
        )
    elif method == "tasks/get":
        task_id = request["params"]["taskId"]
        tasks = _load_tasks()
        task = tasks[task_id]
        if task["status"] == "working" and task["path"] != "wait":
            task["status"] = "completed"
            task["status_message"] = "fixture task completed"
            task["updated_at"] = datetime.now(UTC).isoformat()
            _save_tasks(tasks)
        _result(request_id, _task_view(task_id, task))
    elif method == "tasks/result":
        task_id = request["params"]["taskId"]
        task = _load_tasks()[task_id]
        output = {"path": task["path"], "content": "fixture task evidence"}
        _result(
            request_id,
            {
                "content": [{"type": "text", "text": json.dumps(output)}],
                "structuredContent": output,
                "isError": False,
            },
        )
    elif method == "tasks/cancel":
        task_id = request["params"]["taskId"]
        tasks = _load_tasks()
        task = tasks[task_id]
        task["status"] = "cancelled"
        task["status_message"] = "fixture task cancelled"
        task["updated_at"] = datetime.now(UTC).isoformat()
        _save_tasks(tasks)
        _result(request_id, _task_view(task_id, task))
