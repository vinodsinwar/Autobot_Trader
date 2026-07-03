# ⚡ Autobot Trader

A self-hosted automated algo-trading system that listens for trading signals from
**Telegram channels** and **YouTube live streams**, parses them instantly, and executes
bracket orders on **Dhan** (Indian equity/F&O) and **Delta Exchange India** (crypto) —
with a full control dashboard, risk engine, audit trail, and P&L reporting.

```
SOURCE 1: Telegram channels ──Telethon────────────────────────────┐
                                                                  ▼
SOURCE 2: YouTube live ─► LiveWatcher ─► ffmpeg ─► STT ─► LLM Extractor
          (yt-dlp poll)    (live edge)  (pluggable)  │ (per-channel calibration)
                                                     ▼
                                     relay → your Telegram signal group
                                                                  │
                                                                  ▼
                     Ingestion ─► Parser ─► Risk Engine ─► Order Router
                                (rules→LLM)  (kill switch,   ├─ Dhan (Super Order)
                                              caps, hours,   ├─ Delta India (bracket)
                                              approvals)     └─ Paper (default)
                                                                  │
             React dashboard ◄── WebSocket ◄── tracker ◄── broker order streams
```

## Feature highlights

- **Telegram ingestion** via your own account (Telethon/MTProto) — works with any channel
  you're a member of; message **edits** modify the live bracket (new SL/target) instead of
  re-entering.
- **Hybrid parsing**: deterministic per-channel rule packs (sub-millisecond) with an
  optional **LLM fallback — any provider/model via litellm** (Anthropic, OpenAI, Gemini,
  Ollama, …), selected in Settings. LLM output must pass strict schema + self-consistency
  validation (SL below entry for longs, etc.) or the signal is refused.
- **YouTube live signals**: subscribe to any trader's channel; when they go live the audio
  is captured at the live edge, transcribed by a **pluggable STT engine** (Deepgram
  streaming recommended; OpenAI or local faster-whisper supported), and a two-tier
  extractor (instant lexicon spotter + fast-LLM confirmation) emits signals with
  per-stage latency stamps. System-added latency ≈ 3–6s on top of YouTube's own
  broadcast delay.
- **Calibration studio**: paste a channel's past stream URLs → batch transcribe → review
  extracted candidates → mark **correct / wrong / missed**. Feedback compiles into
  per-channel few-shot examples + spotter patterns with measured precision/recall —
  provider-agnostic "training" that improves both accuracy and latency.
- **Execution**: Dhan **Super Orders** (entry+target+SL+trailing in one request), Delta
  **bracket orders**, or the built-in **paper broker** (default). Fill tracking over each
  broker's order websocket; restart **reconciliation** against the broker book prevents
  phantom/duplicate orders.
- **Risk engine** (all dashboard-editable): global kill switch, paper/live master toggle,
  max trades/day, max concurrent positions, max capital per trade, **daily-loss
  circuit breaker**, IST trading-hours windows, duplicate-signal suppression, per-channel
  manual-approval gates (YouTube defaults to manual).
- **Tracking & reporting**: every message, parse, decision, order, and fill is persisted
  with an append-only audit log; dashboard shows live P&L, daily/cumulative charts,
  per-source win rates, and CSV export.

## Quickstart (local, zero external deps)

```bash
# 1. backend
cd backend
python3.11 -m venv .venv && .venv/bin/pip install -e ".[dev]"
cp ../.env.example ../.env
# REQUIRED — generate the secrets master key and put it in .env:
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
.venv/bin/python -m uvicorn app.main:app --port 8000   # SQLite by default

# 2. dashboard (dev mode, hot reload, proxies /api to :8000)
cd ../frontend
npm install && npm run dev          # open http://localhost:5173
# (or `npm run build` — the backend then serves it at http://localhost:8000)
```

Log in with `AUTOBOT_ADMIN_PASSWORD` (default `change-me` — change it immediately in
Settings). ffmpeg must be on PATH for the YouTube pipeline (`apt install ffmpeg`).

## Production (Docker on a VPS)

```bash
cp .env.example .env    # set AUTOBOT_MASTER_KEY, AUTOBOT_ADMIN_PASSWORD, service toggles
docker compose up -d    # app (dashboard on :8000) + Postgres
```

Put a TLS reverse proxy (Caddy/nginx) in front of port 8000 — the dashboard controls
real money. The compose file pins data to named volumes; yt-dlp breaks occasionally when
YouTube changes internals — rebuild the image (`docker compose build --no-cache app`) to
pick up the latest.

## Setup walkthrough

1. **Telegram** — get `api_id`/`api_hash` from [my.telegram.org](https://my.telegram.org),
   then on the server run `python -m app.ingestion.tg_login` (one-time OTP login; the
   session is stored encrypted). Set `AUTOBOT_ENABLE_TELEGRAM=true` and restart.
2. **Channels** — dashboard → *Telegram Channels* → add by `@username`/link. Pick the
   broker, rule pack, sizing, and auto/manual execution. Use the built-in **test bench**
   to paste real messages from the channel and see exactly how they parse before enabling.
3. **LLM (optional but recommended)** — Settings → LLM: any litellm model string + key.
   Also set a cheap fast model for the YouTube live path.
4. **Brokers** —
   - *Dhan*: client ID + access token from web.dhan.co → DhanHQ APIs. **Tokens last 24h
     (SEBI rule)** — the dashboard shows a countdown; paste a fresh token daily. Click
     *Sync instruments* (scrip master) once, it refreshes daily after that.
   - *Delta India*: API key/secret; tick **testnet** first and place real test orders
     against the India testnet. Click *Sync products*.
5. **YouTube** — Settings → STT (Deepgram key recommended), then *YouTube* page →
   subscribe with the channel URL. Feed it 3–5 past stream VODs in the calibration studio
   and label the results before trusting it live. Optionally set Settings key `youtube`
   → `relay_chat_id` so every detected signal is posted to your Telegram group.
6. **Risk** — review every limit in Settings → Risk. `live_trading` stays **off** until
   you flip it: with it off, every order goes to the paper broker no matter what.
7. **Tracker** — set `AUTOBOT_ENABLE_TRACKER=true` (fill tracking + restart
   reconciliation), `AUTOBOT_ENABLE_YOUTUBE=true` for the YouTube service,
   `AUTOBOT_ENABLE_SCRIP_SYNC=true` for daily instrument refresh.

## Go-live checklist

1. Run in **paper mode** for at least a week; check per-source win rates in Reports.
2. Verify crypto end-to-end on the **Delta testnet** (real API, fake money).
3. Keep **manual approval** on for every channel until its parses are consistently right.
4. Flip `live_trading` with the **smallest sizing** (1 lot / minimal contracts).
5. Know where the **kill switch** is (bottom-left, every page). The daily-loss breaker
   halts trading automatically at your configured loss.

## Development

```bash
cd backend && .venv/bin/python -m pytest tests -q   # 111 tests
.venv/bin/ruff check app tests                      # lint
cd frontend && npm run build                        # tsc strict + vite
```

Project layout: `backend/app/{ingestion,parsing,risk,execution,instruments,youtube,api,reporting}` —
each subsystem is independently tested; `tests/test_engine.py` covers the full
message→order→fill→P&L path over the paper broker.

## Honest caveats

- **Dhan's 24h token cannot be auto-renewed** (SEBI mandate) — daily paste required.
- **Voice latency floor**: the streamer's own YouTube broadcast delay (2–30s by their
  latency setting) is irreducible; the system adds ~3–6s on top. Fine for level-based
  calls, not for scalping.
- **Mis-heard numbers are the top voice hazard** — hence schema self-consistency checks,
  manual-approval default, the transcript excerpt beside every candidate, and calibration
  precision gating.
- Automating a **user Telegram account** sits in a gray zone of Telegram's ToS; at
  personal scale the practical risk is low.
- SEBI's retail algo framework is evolving — confirm your broker's current API policy
  applies to your account. **This software executes real trades; you are responsible for
  every order it places. No warranty. Trade small first.**
