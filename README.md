# OutboundAI — AI Outbound Voice Calling Platform

Production-grade outbound voice calling SaaS. An AI agent (Google **Gemini Live** native audio) places real phone calls over a **Vobiz SIP** trunk via **LiveKit Cloud**, books appointments, handles objections, transfers to humans, and logs everything to **Supabase**. Ships with a single-file web dashboard.

```
Dashboard (ui/index.html)
        │  REST
        ▼
FastAPI (server.py) ──► LiveKit Cloud ──► Vobiz SIP ──► 📞 phone
        │                     │
        ▼                     ▼
   Supabase            Agent worker (agent.py)
   (db.py)             └─ Gemini Live + tools.py
```

## Components

| File | Purpose |
|------|---------|
| `agent.py` | LiveKit worker `outbound-caller`. Gemini Live realtime session, SIP dial-out, recording egress, keep-alive. |
| `server.py` | FastAPI: call dispatch, stats, appointments, prompt, settings, logs, CRM, agent profiles, campaigns (APScheduler). |
| `db.py` | Supabase data layer + BYOK settings (sensitive keys are write-only). |
| `tools.py` | 9 function tools: availability, booking, end_call, transfer, SMS, lookup, memory, Cal.com. |
| `prompts.py` | Default "Priya" booking prompt + `{lead_name}/{business_name}/{service_type}` interpolation. |
| `ui/index.html` | Single-file dark dashboard (Stats, Single/Batch Call, Campaigns, Agents, Prompt, Appointments, Logs, CRM, Settings, Setup). |
| `supabase_schema.sql` | Full DB schema — run once in Supabase. |

## Prerequisites (BYOK)

| Service | Get keys from |
|---------|---------------|
| LiveKit Cloud | https://cloud.livekit.io → Project → Keys |
| Google Gemini | https://aistudio.google.com/app/apikey |
| Vobiz SIP | https://vobiz.ai |
| Supabase | https://supabase.com → Settings → API |
| Twilio (optional) | SMS confirmations |
| Cal.com (optional) | Calendar sync |
| S3 / Supabase Storage (optional) | Call recordings |

## Setup

1. **Database** — open Supabase → SQL Editor → paste and run `supabase_schema.sql`.
2. **Env** — credentials come **only** from environment variables. For local dev, copy the template and fill it in (the app reads it via `python-dotenv`); on a VPS, set these in your platform's env UI instead:
   ```powershell
   Copy-Item .env.example .env
   ```
3. **Install & run (local):**
   ```powershell
   py -m pip install -r requirements.txt
   # Terminal 1 — API + dashboard
   uvicorn server:app --host 0.0.0.0 --port 8000
   # Terminal 2 — agent worker
   py agent.py start
   ```
   Or both at once via `bash start.sh`.
4. **Dashboard** — open http://localhost:8000
   - **Settings** → verify every credential shows `configured` (read-only — values come from env)
   - **Settings → Vobiz** → **⚡ Create SIP Trunk** → copy the returned ID into `OUTBOUND_TRUNK_ID` env var → restart
   - **AI Prompt** → customise → Save (stored in Supabase)
   - **Agents** → create at least one profile
   - **Single Call** → test with your own number

## Docker / Coolify deploy

```bash
docker compose up -d --build
```

On Coolify (or Dokploy/Portainer): deploy as a Docker Compose / Dockerfile app, expose port **8000**, and set every env var from `.env.example` in the platform's **Environment** UI — that is the single source of truth. No `.env` file is needed or baked into the image. The worker and API both start from `start.sh`.

## Configuration model — environment variables are the single source of truth

**All credentials and service configuration are read exclusively from the process / VPS environment variables. Nothing else.**

- The app **never** reads credentials from the database and **never** writes them there. The dashboard **Settings** tab is **read-only** — it only shows whether each env var is `configured`. Sensitive values are never sent to the browser.
- To change any credential/service setting: update the env var on your VPS (or Coolify/Docker env UI) and **restart** the service.
- A local `.env` is only a dev convenience. `load_dotenv(..., override=False)` guarantees real environment variables always win, and `.dockerignore` keeps `.env` out of the image — so a deployed container uses **only** injected env vars.
- The few things stored in Supabase are genuine **application data**, not environment configuration: the AI system prompt, tool toggles (`ENABLED_TOOLS`), agent profiles, campaigns, appointments, call logs.
- **Creating the SIP trunk** (Settings → ⚡ Create SIP Trunk) returns a trunk ID. Copy it into `OUTBOUND_TRUNK_ID` in your VPS env and restart — it is not auto-persisted anywhere.

### Required env vars

`LIVEKIT_URL` · `LIVEKIT_API_KEY` · `LIVEKIT_API_SECRET` · `GOOGLE_API_KEY` · `VOBIZ_SIP_DOMAIN` · `VOBIZ_USERNAME` · `VOBIZ_PASSWORD` · `VOBIZ_OUTBOUND_NUMBER` · `OUTBOUND_TRUNK_ID` · `SUPABASE_URL` · `SUPABASE_SERVICE_KEY`

Optional: `GEMINI_MODEL`, `GEMINI_TTS_VOICE`, `USE_GEMINI_REALTIME`, `DEFAULT_TRANSFER_NUMBER`, Twilio (`TWILIO_*`), recordings (`S3_*`), Cal.com (`CALCOM_*`), `DEEPGRAM_API_KEY`. See `.env.example` for the full list.

## Behaviour notes

- **Default model** `gemini-3.1-flash-live-preview`. For 3.1/2.5 native-audio models the agent greets autonomously from the system prompt (no `generate_reply` call).
- **Per-call overrides** (voice/model/tools from an agent profile) are passed explicitly into the session — the worker never mutates `os.environ`, so concurrent calls never contaminate each other.
- **Silence prevention** is enabled via session resumption, context-window compression, and low end-of-speech sensitivity (2000 ms).

## Cost (approx, per minute)

| Item | ₹/min |
|------|-------|
| Vobiz SIP | 1.00 |
| LiveKit Cloud | 0.17 |
| Gemini Live | 0.03 |
| **Total** | **≈ 1.20** |

## Endpoints (selected)

`POST /api/call` · `GET /api/stats` · `GET/POST /api/prompt` · `GET/POST /api/settings` · `POST /api/setup/trunk` · `GET/POST/DELETE /api/campaigns` · `GET/POST/PUT/DELETE /api/agent-profiles` · `GET /api/calls` · `GET /api/appointments` · `GET /api/crm` · `GET/DELETE /api/logs`
