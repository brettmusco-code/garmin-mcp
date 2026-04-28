"""MCP JSON-RPC 2.0 server over HTTP for Garmin Connect."""
from __future__ import annotations

import os
import re
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from . import garmin

app = FastAPI()

BEARER = os.environ.get("MCP_BEARER_TOKEN")
if not BEARER:
    raise RuntimeError("MCP_BEARER_TOKEN is required")

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

TOOLS = [
    {
        "name": "get_activities",
        "description": (
            "List recent Garmin activities (runs, rides, walks, etc.). "
            "Returns start time, type, distance, duration, calories."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "start": {"type": "number", "description": "offset, default 0"},
                "limit": {"type": "number", "description": "how many, default 10, max 50"},
            },
        },
    },
    {
        "name": "get_steps",
        "description": "Get step count data for a given date (YYYY-MM-DD).",
        "inputSchema": {
            "type": "object",
            "properties": {"date": {"type": "string", "description": "YYYY-MM-DD"}},
            "required": ["date"],
        },
    },
    {
        "name": "get_sleep",
        "description": "Get sleep data for a given date (YYYY-MM-DD).",
        "inputSchema": {
            "type": "object",
            "properties": {"date": {"type": "string", "description": "YYYY-MM-DD"}},
            "required": ["date"],
        },
    },
    {
        "name": "get_heart_rate",
        "description": "Get heart rate samples for a given date (YYYY-MM-DD).",
        "inputSchema": {
            "type": "object",
            "properties": {"date": {"type": "string", "description": "YYYY-MM-DD"}},
            "required": ["date"],
        },
    },
    {
        "name": "get_user_info",
        "description": "Get the authenticated Garmin user's profile.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def _require_date(args: dict) -> str:
    d = args.get("date")
    if not isinstance(d, str) or not DATE_RE.match(d):
        raise ValueError("`date` must be a string in YYYY-MM-DD format")
    return d


def _call_tool(name: str, args: dict) -> Any:
    if name == "get_activities":
        start = int(args.get("start", 0))
        limit = min(int(args.get("limit", 10)), 50)
        return garmin.get_activities(start, limit)
    if name == "get_steps":
        return garmin.get_steps(_require_date(args))
    if name == "get_sleep":
        return garmin.get_sleep(_require_date(args))
    if name == "get_heart_rate":
        return garmin.get_heart_rate(_require_date(args))
    if name == "get_user_info":
        return garmin.get_user_info()
    raise ValueError(f"Unknown tool: {name}")


def _ok(rpc_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


def _err(rpc_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}}


def _handle(req: dict) -> dict:
    rpc_id = req.get("id")
    method = req.get("method")
    params = req.get("params") or {}
    try:
        if method == "initialize":
            return _ok(
                rpc_id,
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "garmin-mcp", "version": "0.1.0"},
                },
            )
        if method == "tools/list":
            return _ok(rpc_id, {"tools": TOOLS})
        if method == "tools/call":
            name = params.get("name")
            args = params.get("arguments") or {}
            data = _call_tool(name, args)
            import json

            return _ok(
                rpc_id,
                {"content": [{"type": "text", "text": json.dumps(data, default=str, indent=2)}]},
            )
        if method == "ping":
            return _ok(rpc_id, {})
        return _err(rpc_id, -32601, f"Method not found: {method}")
    except Exception as e:  # noqa: BLE001
        return _err(rpc_id, -32000, str(e))


@app.get("/")
@app.get("/health")
def health() -> PlainTextResponse:
    return PlainTextResponse("garmin-mcp ok")


@app.post("/mcp")
async def mcp(request: Request, authorization: str | None = Header(default=None)):
    if authorization != f"Bearer {BEARER}":
        raise HTTPException(status_code=401, detail="Unauthorized")
    body = await request.json()
    if isinstance(body, list):
        out = [_handle(b) for b in body]
    else:
        out = _handle(body)
    return JSONResponse(out)
