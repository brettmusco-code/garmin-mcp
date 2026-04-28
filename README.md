# garmin-mcp

Remote MCP server that wraps Garmin Connect with MFA support. Deploy to Render free tier, add as a Custom Connector in claude.ai, and query your Garmin data from mobile.

## Tools

- `get_activities` — recent activities
- `get_steps` — steps for a date
- `get_sleep` — sleep for a date
- `get_heart_rate` — HR samples for a date
- `get_user_info` — profile

## How auth works

Garmin requires MFA (email code) on most accounts. You can't complete that from a server. Instead:

1. **Once, locally:** run `scripts/bootstrap.py`. You log in, paste the emailed code, and the script prints a base64 blob containing long-lived OAuth tokens.
2. **Deploy:** paste that blob into Render as `GARTH_TOKENS_B64`. The server uses the tokens (no password ever leaves your machine). Tokens auto-refresh for ~1 year.
3. If re-auth is needed later (password change, token expiry), re-run bootstrap.

## One-time local setup

```bash
cd ~/garmin-mcp
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python scripts/bootstrap.py
```

Copy the printed `GARTH_TOKENS_B64` value — you'll need it below.

## Local dev

Fill in `.env`:

```
MCP_BEARER_TOKEN=some-long-random-string
GARTH_TOKENS_B64=<paste-from-bootstrap>
```

Run:

```bash
source .venv/bin/activate
set -a && source .env && set +a
uvicorn app.main:app --reload --port 8787
```

Test:

```bash
TOKEN=$(grep ^MCP_BEARER_TOKEN= .env | cut -d= -f2-)
curl -s -X POST http://localhost:8787/mcp \
  -H "Authorization: Bearer $TOKEN" \
  -H "content-type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"get_user_info","arguments":{}}}' | python3 -m json.tool
```

## Deploy to Render

1. Push this directory to a GitHub repo.
2. https://render.com → New → Web Service → connect your repo.
3. Render auto-detects `render.yaml`. Confirm.
4. Set env vars in Render dashboard:
   - `MCP_BEARER_TOKEN` — same random string from `.env`
   - `GARTH_TOKENS_B64` — the blob from bootstrap
5. Deploy. You'll get a URL like `https://garmin-mcp.onrender.com`.

Free tier sleeps after ~15 min idle. First request after sleep: ~30–60s cold start.

## Add to claude.ai

1. https://claude.ai → Settings → Connectors → Add custom connector
2. URL: `https://<your-service>.onrender.com/mcp`
3. Auth header: `Authorization: Bearer <your MCP_BEARER_TOKEN>`
4. Save. Available on web + iOS + Android.

## Caveats

- **Unofficial Garmin auth.** `python-garminconnect` uses a reverse-engineered flow. Garmin occasionally changes it; check that project's issues if something breaks.
- **Token rotation.** Tokens auto-refresh but expire after ~1 year of non-use, or if you change your Garmin password. Re-run bootstrap to refresh.
- **Security.** The bearer token is the only thing protecting `/mcp` — make it long and random (`openssl rand -hex 32`). Anyone with it can read your Garmin data.
