"""MCP JSON-RPC 2.0 server over HTTP for Garmin Connect.

Includes a minimal OAuth 2.0 stub so claude.ai's Custom Connector can connect.
Since this is a personal, single-tenant server, the OAuth flow is a rubber
stamp: any client can register, authorize returns instantly, and the token
endpoint always hands back the configured MCP_BEARER_TOKEN.
"""
from __future__ import annotations

import json
import os
import re
import secrets
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse

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


# ---------- OAuth 2.0 stub for claude.ai Custom Connectors ----------
# Claude expects RFC 9728 (protected resource metadata) + RFC 8414 (auth
# server metadata) + RFC 7591 (dynamic client registration) + RFC 7636 (PKCE).
# We fake all of it and always return the same bearer token.


def _base_url(request: Request) -> str:
    # Honor the proxy's forwarded scheme/host so URLs are https on Render.
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host", request.url.netloc)
    return f"{proto}://{host}"


@app.get("/.well-known/oauth-protected-resource")
@app.get("/.well-known/oauth-protected-resource/mcp")
def protected_resource_metadata(request: Request):
    base = _base_url(request)
    return JSONResponse(
        {
            "resource": f"{base}/mcp",
            "authorization_servers": [base],
            "bearer_methods_supported": ["header"],
        }
    )


@app.get("/.well-known/oauth-authorization-server")
@app.get("/.well-known/oauth-authorization-server/mcp")
def auth_server_metadata(request: Request):
    base = _base_url(request)
    return JSONResponse(
        {
            "issuer": base,
            "authorization_endpoint": f"{base}/authorize",
            "token_endpoint": f"{base}/token",
            "registration_endpoint": f"{base}/register",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code"],
            "code_challenge_methods_supported": ["S256", "plain"],
            "token_endpoint_auth_methods_supported": ["none", "client_secret_post"],
        }
    )


@app.post("/register")
async def register(request: Request):
    # Accept any registration request; echo a fixed client_id back.
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    return JSONResponse(
        {
            "client_id": "garmin-mcp-client",
            "client_id_issued_at": 0,
            "token_endpoint_auth_method": "none",
            "redirect_uris": body.get("redirect_uris", []),
            "grant_types": ["authorization_code"],
            "response_types": ["code"],
        },
        status_code=201,
    )


@app.get("/authorize")
def authorize(
    redirect_uri: str,
    state: str | None = None,
    response_type: str | None = None,
    client_id: str | None = None,
    code_challenge: str | None = None,
    code_challenge_method: str | None = None,
    scope: str | None = None,
):
    # Rubber-stamp approval: mint a code and redirect straight back.
    code = secrets.token_urlsafe(24)
    params = {"code": code}
    if state:
        params["state"] = state
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(url=f"{redirect_uri}{sep}{urlencode(params)}", status_code=302)


@app.post("/token")
async def token(
    grant_type: str = Form(...),
    code: str | None = Form(default=None),
    redirect_uri: str | None = Form(default=None),
    client_id: str | None = Form(default=None),
    code_verifier: str | None = Form(default=None),
    refresh_token: str | None = Form(default=None),
):
    if grant_type not in ("authorization_code", "refresh_token"):
        raise HTTPException(status_code=400, detail="unsupported_grant_type")
    return JSONResponse(
        {
            "access_token": BEARER,
            "token_type": "Bearer",
            "expires_in": 60 * 60 * 24 * 365,  # 1 year
            "refresh_token": BEARER,
            "scope": "mcp",
        }
    )


# ---------- MCP endpoint ----------


@app.post("/mcp")
async def mcp(request: Request, authorization: str | None = Header(default=None)):
    if authorization != f"Bearer {BEARER}":
        # Per RFC 9728, point clients at the protected resource metadata.
        base = _base_url(request)
        return JSONResponse(
            {"error": "invalid_token"},
            status_code=401,
            headers={
                "WWW-Authenticate": (
                    f'Bearer resource_metadata="{base}/.well-known/oauth-protected-resource"'
                )
            },
        )
    body = await request.json()
    if isinstance(body, list):
        out = [_handle(b) for b in body]
    else:
        out = _handle(body)
    return JSONResponse(out)
