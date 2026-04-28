"""Run the MCP server locally, loading env from .env via python-dotenv.

Using this instead of `set -a && source .env` avoids shell-level issues with
long base64 values in GARTH_TOKENS_B64.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

project_root = Path(__file__).resolve().parent.parent
os.chdir(project_root)
sys.path.insert(0, str(project_root))
load_dotenv(project_root / ".env")

import uvicorn  # noqa: E402

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",
        port=int(os.environ.get("PORT", 8787)),
        log_level="warning",
    )
