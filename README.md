# GCP Token Bench

A web-based benchmarking and testing tool for Google Gemini AI models via the Vertex AI API.

Test, compare, and estimate costs across Gemini model variants — including text chat, text-to-speech, and image generation — all from a single interface.

## Features

- **11 Gemini models** — chat, TTS, and image generation variants with real-time pricing
- **Streaming responses** — real-time output via Server-Sent Events
- **Token tracking** — input/output token counts and cost estimation per message
- **Chat history** — persistent conversations with per-chat settings
- **File attachments** — images, audio, video, and PDFs as inline data
- **Advanced config** — temperature, penalties, thinking budget, structured output schemas, TTS voices
- **Google Search & Code Execution** — built-in tool toggles
- **Per-user isolation** — each user uploads their own GCP service account key
- **Dark/light theme**

## Supported Models

| Model | Category | Input $/M | Output $/M |
|-------|----------|-----------|------------|
| Gemini 3.1 Pro | Best reasoning | $2.00 | $12.00 |
| Gemini 3 Flash | Fast + smart | $0.50 | $3.00 |
| Gemini 3.1 Flash Lite | Cheapest 3.x | $0.25 | $1.50 |
| Gemini 2.5 Pro | Stable reasoning | $1.25 | $10.00 |
| Gemini 2.5 Flash | Best value | $0.30 | $2.50 |
| Gemini 2.5 Flash Lite | Cheapest | $0.10 | $0.40 |
| TTS (Flash / Pro) | Text-to-Speech | $0.30-1.25 | $2.50-10.00 |
| Image Gen (Flash / Pro) | Image Generation | $0.30-2.00 | $2.50-12.00 |

## Quick Start (Local Development)

**Prerequisites:** Python 3.10+, a GCP project with Vertex AI API enabled, a service account key with Vertex AI User role.

```bash
git clone https://github.com/galencky/GCP-tokenbench.git
cd GCP-tokenbench

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

JWT_SECRET="dev-secret" DEV_LOGIN=true python app.py
```

Open `http://localhost:5000`, click Dev Login, upload your service account key, and start chatting.

> **macOS note:** Port 5000 is used by AirPlay Receiver. If you get a 403, disable it in System Settings or run on another port.

## Deploy to Vercel

See **[DEPLOY.md](DEPLOY.md)** for the full step-by-step guide covering:

1. Google Cloud project + OAuth setup
2. GCP service account creation
3. Vercel deploy with Neon Postgres (built-in, no external DB needed)
4. Environment variable configuration

## Architecture

```
Browser (SPA)          Flask Backend           Vertex AI API
index.html        -->  app.py            -->  streamGenerateContent
Google Sign-In         JWT auth                Regional/global endpoints
Chat UI + Settings     Neon Postgres           Gemini models
marked.js              Fernet encryption       Streaming JSON
```

- **Backend:** Flask, JWT auth, Neon Postgres (prod) / local files (dev)
- **Frontend:** Single-file SPA (HTML + CSS + JS), no build step
- **Deployment:** Vercel serverless functions

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `POSTGRES_URL` | Prod | Neon Postgres connection string (auto-set by Vercel) |
| `JWT_SECRET` | Yes | Secret for signing JWT tokens |
| `ENCRYPTION_KEY` | Recommended | Fernet key for encrypting SA keys at rest |
| `GOOGLE_CLIENT_ID` | Prod | Google OAuth 2.0 Client ID |
| `DEV_LOGIN` | No | Set `true` for local dev login without Google |
| `ALLOWED_ORIGINS` | Prod | Comma-separated CORS origins |

## Documentation

- **[DEPLOY.md](DEPLOY.md)** — Deployment guide (Vercel + Neon + Google OAuth)
- **[document.md](document.md)** — Technical reference (architecture, API contracts, internals)
