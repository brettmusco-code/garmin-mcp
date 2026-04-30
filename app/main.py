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

from . import cache, garmin

app = FastAPI()

BEARER = os.environ.get("MCP_BEARER_TOKEN")
if not BEARER:
    raise RuntimeError("MCP_BEARER_TOKEN is required")

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

DAILY_METRIC_KEYS = sorted(garmin.DAILY_METHODS.keys())

TOOLS = [
    {
        "name": "get_activities",
        "description": (
            "List Garmin activities (runs, rides, walks, etc.). Two modes:\n"
            "- Recent mode: pass `start` (offset) and/or `limit` (default 10, max 50). "
            "Returns newest-first.\n"
            "- Date range mode: pass `startdate` + `enddate` (max 366 days, inclusive). "
            "Optionally filter by `activity_type` (e.g., 'running', 'cycling').\n"
            "Either mode returns start time, type, distance, duration, calories. "
            "Range-mode results are cached in S3; pass `force_refresh=true` to bypass."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "start": {"type": "number", "description": "offset for recent mode, default 0"},
                "limit": {"type": "number", "description": "for recent mode: default 10, max 50"},
                "startdate": {"type": "string", "description": "YYYY-MM-DD (range mode)"},
                "enddate": {"type": "string", "description": "YYYY-MM-DD (range mode)"},
                "activity_type": {"type": "string", "description": "range mode only: optional filter"},
                "force_refresh": {"type": "boolean", "description": "skip cache, default false"},
            },
        },
    },
    {
        "name": "get_activity_details",
        "description": (
            "Get full details for a single activity: summary, splits, HR zones, "
            "weather, gear. Use the activityId from get_activities. Cached in S3; "
            "pass `force_refresh=true` to bypass."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "activity_id": {"type": "string"},
                "force_refresh": {"type": "boolean", "description": "skip cache, default false"},
            },
            "required": ["activity_id"],
        },
    },
    {
        "name": "get_daily_summaries",
        "description": (
            "Bulk-fetch one or more per-day metrics across a date range (max 366 days). "
            "Large ranges fan out slowly (2 concurrent requests) to avoid Garmin rate limits — "
            "a full year across 5 metrics takes ~15-25 min on a cold cache. "
            "Per (metric, date) is cached in S3, so re-calls for overlapping ranges are near-instant. "
            "Pass `force_refresh=true` to re-fetch from Garmin. "
            "Returns { metric: { date: data } }. Supported metrics: "
            + ", ".join(DAILY_METRIC_KEYS)
            + "."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "startdate": {"type": "string", "description": "YYYY-MM-DD"},
                "enddate": {"type": "string", "description": "YYYY-MM-DD"},
                "metrics": {
                    "type": "array",
                    "items": {"type": "string", "enum": DAILY_METRIC_KEYS},
                    "description": "list of metrics to fetch",
                },
                "force_refresh": {"type": "boolean", "description": "skip cache, default false"},
            },
            "required": ["startdate", "enddate", "metrics"],
        },
    },
    {
        "name": "get_body_composition",
        "description": "Weight, body fat, BMI, muscle mass. Single date or range (max 366 days).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "startdate": {"type": "string", "description": "YYYY-MM-DD"},
                "enddate": {"type": "string", "description": "YYYY-MM-DD (optional)"},
            },
            "required": ["startdate"],
        },
    },
    {
        "name": "get_training_score",
        "description": (
            "Hill or endurance training score. Single date or range (max 366 days). "
            "metric: 'hill' or 'endurance'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "metric": {"type": "string", "enum": ["hill", "endurance"]},
                "startdate": {"type": "string", "description": "YYYY-MM-DD"},
                "enddate": {"type": "string", "description": "YYYY-MM-DD (optional)"},
            },
            "required": ["metric", "startdate"],
        },
    },
    {
        "name": "get_lactate_threshold",
        "description": (
            "Lactate threshold heart rate / pace. Call with no args for latest; or "
            "provide startdate+enddate (max 366 days) for history. aggregation = 'daily' (default) or 'weekly'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "startdate": {"type": "string", "description": "YYYY-MM-DD (optional)"},
                "enddate": {"type": "string", "description": "YYYY-MM-DD (optional)"},
                "aggregation": {"type": "string", "enum": ["daily", "weekly"]},
            },
        },
    },
    {
        "name": "get_progress_summary",
        "description": (
            "Aggregated training progress between two dates (max 366 days). "
            "metric: one of distance, duration, elevationGain, calories (default: distance)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "startdate": {"type": "string", "description": "YYYY-MM-DD"},
                "enddate": {"type": "string", "description": "YYYY-MM-DD"},
                "metric": {"type": "string", "description": "distance/duration/elevationGain/calories"},
                "group_by_activities": {"type": "boolean"},
            },
            "required": ["startdate", "enddate"],
        },
    },
    {
        "name": "get_weekly_summaries",
        "description": (
            "Weekly aggregates (steps, stress, intensity_minutes) ending on a given date. "
            "Up to 104 weeks back. Returns { metric: [...weeks] }."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "enddate": {"type": "string", "description": "YYYY-MM-DD"},
                "weeks": {"type": "number", "description": "1–104, default 52"},
                "metrics": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["steps", "stress", "intensity_minutes"]},
                },
            },
            "required": ["enddate"],
        },
    },
    {
        "name": "get_devices",
        "description": "List registered Garmin devices.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_workouts",
        "description": "List saved/custom workouts in the user's library.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "start": {"type": "number", "description": "offset, default 0"},
                "limit": {"type": "number", "description": "1-100, default 100"},
            },
        },
    },
    {
        "name": "get_workout_by_id",
        "description": "Full step-by-step definition of one saved workout.",
        "inputSchema": {
            "type": "object",
            "properties": {"workout_id": {"type": "string"}},
            "required": ["workout_id"],
        },
    },
    {
        "name": "get_training_plans",
        "description": "Active and available training plans (Garmin Coach + custom).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_training_plan_by_id",
        "description": (
            "Details of one training plan. Set adaptive=true for Garmin Coach "
            "adaptive plans (uses a different Garmin endpoint)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "plan_id": {"type": "string"},
                "adaptive": {"type": "boolean", "description": "default false"},
            },
            "required": ["plan_id"],
        },
    },
    {
        "name": "analyze_training_period",
        "description": (
            "Summarize activities in a date range (max 366 days). Returns totals, "
            "per-activity-type breakdown, and weekly timeline — pre-computed so "
            "you don't have to crunch raw activity JSON."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "startdate": {"type": "string", "description": "YYYY-MM-DD"},
                "enddate": {"type": "string", "description": "YYYY-MM-DD"},
            },
            "required": ["startdate", "enddate"],
        },
    },
    {
        "name": "compare_activities",
        "description": (
            "Side-by-side comparison of 2-10 activities. Returns normalized rows "
            "and deltas-vs-baseline (first id) for distance, pace, HR, calories, "
            "training effect."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "activity_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "2-10 activity IDs (as strings)",
                },
            },
            "required": ["activity_ids"],
        },
    },
    {
        "name": "analyze_sleep_trend",
        "description": (
            "Sleep summary over the last N days (1-180) ending on enddate. "
            "Returns averages (duration/score/stages), simple first-half vs "
            "second-half trend, and per-day series."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "enddate": {"type": "string", "description": "YYYY-MM-DD"},
                "days": {"type": "number", "description": "1-180, default 30"},
            },
            "required": ["enddate"],
        },
    },
    {
        "name": "get_personal_records",
        "description": "Personal records across all activity types (fastest mile, longest ride, etc.).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_race_predictions",
        "description": (
            "Predicted race times (5K/10K/half/full). Optional date range for history; "
            "otherwise returns latest."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "startdate": {"type": "string", "description": "YYYY-MM-DD (optional)"},
                "enddate": {"type": "string", "description": "YYYY-MM-DD (optional)"},
            },
        },
    },
]


def _require(args: dict, key: str) -> str:
    v = args.get(key)
    if not isinstance(v, str) or not DATE_RE.match(v):
        raise ValueError(f"`{key}` must be YYYY-MM-DD")
    return v


def _call_tool(name: str, args: dict) -> Any:
    if name == "get_activities":
        sd, ed = args.get("startdate"), args.get("enddate")
        if sd or ed:
            return garmin.get_activities_in_range(
                _require(args, "startdate"),
                _require(args, "enddate"),
                args.get("activity_type"),
                force_refresh=bool(args.get("force_refresh", False)),
            )
        start = int(args.get("start", 0))
        limit = min(int(args.get("limit", 10)), 50)
        return garmin.get_activities(start, limit)
    if name == "get_activity_details":
        aid = args.get("activity_id")
        if not aid:
            raise ValueError("`activity_id` is required")
        return garmin.get_activity_details(aid, force_refresh=bool(args.get("force_refresh", False)))
    if name == "get_daily_summaries":
        metrics = args.get("metrics")
        if not isinstance(metrics, list) or not metrics:
            raise ValueError("`metrics` must be a non-empty array")
        return garmin.get_daily_summaries(
            _require(args, "startdate"),
            _require(args, "enddate"),
            metrics,
            force_refresh=bool(args.get("force_refresh", False)),
        )
    if name == "get_body_composition":
        s = _require(args, "startdate")
        e = args.get("enddate")
        if e and not DATE_RE.match(e):
            raise ValueError("`enddate` must be YYYY-MM-DD")
        return garmin.get_body_composition(s, e)
    if name == "get_training_score":
        metric = args.get("metric")
        if metric not in ("hill", "endurance"):
            raise ValueError("`metric` must be 'hill' or 'endurance'")
        s = _require(args, "startdate")
        e = args.get("enddate")
        if e and not DATE_RE.match(e):
            raise ValueError("`enddate` must be YYYY-MM-DD")
        return garmin.get_training_score(metric, s, e)
    if name == "get_lactate_threshold":
        s = args.get("startdate")
        e = args.get("enddate")
        if (s and not DATE_RE.match(s)) or (e and not DATE_RE.match(e)):
            raise ValueError("dates must be YYYY-MM-DD")
        agg = args.get("aggregation", "daily")
        if agg not in ("daily", "weekly"):
            raise ValueError("`aggregation` must be 'daily' or 'weekly'")
        return garmin.get_lactate_threshold(s, e, agg)
    if name == "get_progress_summary":
        return garmin.get_progress_summary(
            _require(args, "startdate"),
            _require(args, "enddate"),
            args.get("metric", "distance"),
            bool(args.get("group_by_activities", True)),
        )
    if name == "get_weekly_summaries":
        weeks = int(args.get("weeks", 52))
        metrics = args.get("metrics")
        if metrics is not None and not isinstance(metrics, list):
            raise ValueError("`metrics` must be an array")
        return garmin.get_weekly_summaries(_require(args, "enddate"), weeks, metrics)
    if name == "get_devices":
        return garmin.get_devices()
    if name == "get_workouts":
        return garmin.get_workouts(int(args.get("start", 0)), int(args.get("limit", 100)))
    if name == "get_workout_by_id":
        wid = args.get("workout_id")
        if not wid:
            raise ValueError("`workout_id` is required")
        return garmin.get_workout_by_id(wid)
    if name == "get_training_plans":
        return garmin.get_training_plans()
    if name == "get_training_plan_by_id":
        pid = args.get("plan_id")
        if not pid:
            raise ValueError("`plan_id` is required")
        return garmin.get_training_plan_by_id(pid, bool(args.get("adaptive", False)))
    if name == "analyze_training_period":
        return garmin.analyze_training_period(
            _require(args, "startdate"), _require(args, "enddate")
        )
    if name == "compare_activities":
        ids = args.get("activity_ids")
        if not isinstance(ids, list) or not (2 <= len(ids) <= 10):
            raise ValueError("`activity_ids` must be an array of 2-10 items")
        return garmin.compare_activities(ids)
    if name == "analyze_sleep_trend":
        days = int(args.get("days", 30))
        return garmin.analyze_sleep_trend(_require(args, "enddate"), days)
    if name == "get_personal_records":
        return garmin.get_personal_records()
    if name == "get_race_predictions":
        s = args.get("startdate")
        e = args.get("enddate")
        if (s and not DATE_RE.match(s)) or (e and not DATE_RE.match(e)):
            raise ValueError("dates must be YYYY-MM-DD")
        return garmin.get_race_predictions(s, e)
    raise ValueError(f"Unknown tool: {name}")


RESOURCES = [
    {
        "uri": "garmin://athlete/profile",
        "name": "Athlete Profile",
        "description": "Authenticated Garmin user's profile, settings, unit system, full name.",
        "mimeType": "application/json",
    },
    {
        "uri": "garmin://today/summary",
        "name": "Today's Summary",
        "description": "Today's stats, steps, sleep, HR, body battery — live context for the current day.",
        "mimeType": "application/json",
    },
    {
        "uri": "garmin://training/readiness",
        "name": "Training Readiness",
        "description": "Today's training readiness, status, HRV, and morning readiness score.",
        "mimeType": "application/json",
    },
]

RESOURCE_READERS = {
    "garmin://athlete/profile": lambda: garmin.resource_athlete_profile(),
    "garmin://today/summary": lambda: garmin.resource_today_summary(),
    "garmin://training/readiness": lambda: garmin.resource_training_readiness(),
}


def _ok(rpc_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


def _err(rpc_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}}


def _exception_to_rpc_error(rpc_id: Any, ex: Exception) -> dict:
    if isinstance(ex, garmin.GarminAuthError):
        return _err(rpc_id, -32001, f"Garmin auth failed: {ex}")
    if isinstance(ex, garmin.GarminRateLimitError):
        return _err(rpc_id, -32002, f"Garmin rate limited (retry later): {ex}")
    if isinstance(ex, garmin.GarminNotFoundError):
        return _err(rpc_id, -32003, f"Garmin not found: {ex}")
    return _err(rpc_id, -32000, str(ex))


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
                    "capabilities": {"tools": {}, "resources": {}},
                    "serverInfo": {"name": "garmin-mcp", "version": "0.3.0"},
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
        if method == "resources/list":
            return _ok(rpc_id, {"resources": RESOURCES})
        if method == "resources/read":
            uri = params.get("uri")
            reader = RESOURCE_READERS.get(uri)
            if reader is None:
                raise ValueError(f"Unknown resource: {uri}")
            data = reader()
            return _ok(
                rpc_id,
                {
                    "contents": [
                        {
                            "uri": uri,
                            "mimeType": "application/json",
                            "text": json.dumps(data, default=str, indent=2),
                        }
                    ]
                },
            )
        if method == "ping":
            return _ok(rpc_id, {})
        return _err(rpc_id, -32601, f"Method not found: {method}")
    except Exception as e:  # noqa: BLE001
        return _exception_to_rpc_error(rpc_id, e)


@app.get("/")
@app.get("/health")
def health() -> PlainTextResponse:
    return PlainTextResponse("garmin-mcp ok")


@app.get("/cache/list")
def cache_list(tool: str | None = None, limit: int = 100) -> JSONResponse:
    """List cached keys under the configured prefix (or under a tool subprefix)."""
    try:
        keys = cache.list_keys(tool, limit)
        return JSONResponse({"count": len(keys), "keys": keys})
    except Exception as ex:  # noqa: BLE001
        return JSONResponse(
            {"error": f"{type(ex).__name__}: {ex}"}, status_code=500
        )


@app.get("/cache/count")
def cache_count(tool: str | None = None) -> JSONResponse:
    """Total count of cached keys (paginates beyond 1000)."""
    try:
        return JSONResponse({"count": cache.count_keys(tool)})
    except Exception as ex:  # noqa: BLE001
        return JSONResponse(
            {"error": f"{type(ex).__name__}: {ex}"}, status_code=500
        )


@app.post("/cache/delete")
def cache_delete(
    tool: str,
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    """Delete all cached objects for a given tool prefix. Auth required."""
    if authorization != f"Bearer {BEARER}":
        raise HTTPException(status_code=401, detail="unauthorized")
    try:
        deleted = cache.delete_prefix(tool)
        return JSONResponse({"tool": tool, "deleted": deleted})
    except Exception as ex:  # noqa: BLE001
        return JSONResponse(
            {"error": f"{type(ex).__name__}: {ex}"}, status_code=500
        )


@app.get("/cache/health")
def cache_health() -> JSONResponse:
    """Diagnose cache config. Public — only exposes config values (no secrets)
    and a roundtrip probe result. Useful for debugging R2/S3 setup."""
    info: dict[str, Any] = {
        "enabled": cache.enabled(),
        "bucket": cache.BUCKET,
        "endpoint_url": cache.ENDPOINT_URL,
        "region": cache.REGION,
        "prefix": cache.PREFIX,
        "ttl_seconds": cache.DEFAULT_TTL_SECONDS,
    }
    if not cache.enabled():
        info["status"] = "disabled (S3_CACHE_BUCKET not set)"
        return JSONResponse(info)
    probe_args = {"probe": "__cache_health__"}
    try:
        cache.put("__cache_health__", probe_args, {"ok": True}, raise_on_error=True)
        info["probe_write"] = "ok"
    except Exception as ex:  # noqa: BLE001
        info["status"] = "write_failed"
        info["error"] = f"{type(ex).__name__}: {ex}"
        return JSONResponse(info, status_code=500)
    try:
        got = cache.get("__cache_health__", probe_args, raise_on_error=True)
        info["probe_read"] = "ok" if got and got.get("ok") is True else f"unexpected: {got}"
        info["status"] = "ok"
    except Exception as ex:  # noqa: BLE001
        info["status"] = "read_failed"
        info["error"] = f"{type(ex).__name__}: {ex}"
        return JSONResponse(info, status_code=500)
    return JSONResponse(info)


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
