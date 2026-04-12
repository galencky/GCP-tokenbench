# GCP Token Bench — Project Documentation

> **Purpose:** Complete technical reference for LLM-assisted development handover.
> **Last updated:** 2026-04-13
> **Repository:** https://github.com/galencky/GCP-tokenbench.git

---

## Table of Contents

1. [Project Overview](#1-project-overview)
2. [Architecture](#2-architecture)
3. [File Structure](#3-file-structure)
4. [Backend — app.py](#4-backend--apppy)
   - [Models Registry](#41-models-registry)
   - [Routes Reference](#42-routes-reference)
   - [Authentication & Key Management](#43-authentication--key-management)
   - [Chat Endpoint (Core Logic)](#44-chat-endpoint-core-logic)
   - [Streaming JSON Parser](#45-streaming-json-parser)
   - [PCM-to-WAV Conversion](#46-pcm-to-wav-conversion)
   - [Media Storage](#47-media-storage)
   - [Path Traversal Protection](#48-path-traversal-protection)
5. [Frontend — index.html](#5-frontend--indexhtml)
   - [Global State](#51-global-state)
   - [Auth Flow](#52-auth-flow)
   - [Toggle System & Mutual Exclusivity](#53-toggle-system--mutual-exclusivity)
   - [Settings & Generation Config](#54-settings--generation-config)
   - [Chat Flow (send → stream → render)](#55-chat-flow-send--stream--render)
   - [Media Handling](#56-media-handling)
   - [Chat History Management](#57-chat-history-management)
   - [Theme System](#58-theme-system)
   - [CSS Architecture](#59-css-architecture)
6. [Data Storage & File Formats](#6-data-storage--file-formats)
7. [API Contract Reference](#7-api-contract-reference)
8. [Dependencies](#8-dependencies)
9. [How to Run](#9-how-to-run)
10. [Known Quirks & Edge Cases](#10-known-quirks--edge-cases)
11. [Git History & Evolution](#11-git-history--evolution)
12. [Security Considerations](#12-security-considerations)

---

## 1. Project Overview

**GCP Token Bench** is a web-based benchmarking and testing tool for Google's Gemini AI models via the Vertex AI API. It provides:

- Interactive chat with 11 Gemini model variants (text, TTS, image generation)
- Real-time streaming responses via Server-Sent Events
- Token usage tracking and cost estimation
- Persistent chat history with settings per-chat
- File attachments (images, audio, video, PDF)
- Per-user service account isolation
- Dark/light theme

The app is a **Flask backend** serving a **single-page HTML/JS/CSS frontend** with no build step.

---

## 2. Architecture

```
┌─────────────────────────────────────────────────────────┐
│  Browser (Single Page App)                              │
│  templates/index.html                                   │
│  - Google Sign-In / dev bypass                          │
│  - Chat UI with streaming display                       │
│  - Settings panel (generation config, toggles)          │
│  - marked.js for markdown rendering                     │
└──────────────────┬──────────────────────────────────────┘
                   │  HTTP (localhost:5000)
                   │  SSE streaming for /api/chat
┌──────────────────▼──────────────────────────────────────┐
│  Flask Backend (app.py)                                 │
│  - Routes: auth, chat, history, media                   │
│  - JWT auth + encrypted SA key storage                  │
│  - Streams Vertex AI response as SSE                    │
│  - Neon Postgres (prod) / local files (dev)             │
└──────────────────┬──────────────────────────────────────┘
                   │  HTTPS (Bearer token auth)
                   │  streamGenerateContent
┌──────────────────▼──────────────────────────────────────┐
│  Google Vertex AI API                                   │
│  - Regional: {location}-aiplatform.googleapis.com       │
│  - Global:   aiplatform.googleapis.com                  │
│  - v1beta1 endpoint                                     │
└─────────────────────────────────────────────────────────┘
```

**Data flow for a chat message:**
1. User types prompt → JS builds `{messages, generationConfig, toggles}` → POST `/api/chat`
2. Flask loads user's SA key → gets fresh access token → builds Vertex AI request body
3. Flask POSTs to Vertex AI `streamGenerateContent` with `stream=True`
4. Flask parses chunked JSON array → yields SSE events (`data: {...}\n\n`)
5. JS reads SSE stream → updates DOM in real-time (text, images, audio, code blocks)
6. On stream end → JS renders final markdown, saves media refs, auto-saves chat

---

## 3. File Structure

```
GCP-tokenbench/
├── app.py                          # Flask backend (≈800 lines)
├── requirements.txt                # Python dependencies (7 packages)
├── vercel.json                     # Vercel deployment config
├── templates/
│   └── index.html                  # Full SPA frontend (≈1085 lines)
├── DEPLOY.md                       # Step-by-step deployment guide
├── document.md                     # Technical reference (this file)
├── .env.example                    # Environment variable template
├── local_data/                     # Dev-only runtime data (gitignored)
│   └── users/
│       └── {sha256_hash[:16]}/     # Per-user directory
│           ├── sa_key.enc          # Encrypted service account key
│           ├── config.json         # {project_id, client_email, updated_at}
│           ├── chats/
│           │   └── {chat_id}.json  # Chat history + settings
│           └── media/
│               └── {media_id}.json # {data: base64, mimeType}
├── api_key.json                    # Dev SA key (gitignored)
└── .gitignore
```

---

## 4. Backend — app.py

### 4.1 Models Registry

11 models defined in the `MODELS` dict. Each entry has:

| Field | Type | Description |
|-------|------|-------------|
| `name` | str | Display name |
| `input_price` | float | USD per million input tokens |
| `output_price` | float | USD per million output tokens |
| `context` | int | Max context window (tokens) |
| `category` | str | UI category label |
| `global` | bool | `True` = global endpoint, `False` = regional |
| `group` | str? | `"tts"`, `"image"`, or absent (= chat) |

**Model list:**

| ID | Category | In/Out $/M | Context | Global | Group |
|----|----------|-----------|---------|--------|-------|
| `gemini-3.1-pro-preview` | Best reasoning | 2.00/12.00 | 1M | ✓ | chat |
| `gemini-3-flash-preview` | Fast + smart | 0.50/3.00 | 1M | ✓ | chat |
| `gemini-3.1-flash-lite-preview` | Cheapest 3.x | 0.25/1.50 | 1M | ✓ | chat |
| `gemini-2.5-pro` | Stable reasoning | 1.25/10.00 | 1M | ✗ | chat |
| `gemini-2.5-flash` | Best value | 0.30/2.50 | 1M | ✗ | chat |
| `gemini-2.5-flash-lite` | Cheapest | 0.10/0.40 | 1M | ✗ | chat |
| `gemini-2.5-flash-preview-tts` | Text-to-Speech | 0.30/2.50 | **32K** | ✗ | tts |
| `gemini-2.5-pro-preview-tts` | TTS (HD) | 1.25/10.00 | **32K** | ✗ | tts |
| `gemini-2.5-flash-image` | Image Generation | 0.30/2.50 | 1M | ✗ | image |
| `gemini-3.1-flash-image-preview` | Image Gen (3.x) | 0.50/3.00 | 1M | ✓ | image |
| `gemini-3-pro-image-preview` | Image Gen (HD) | 2.00/12.00 | 1M | ✓ | image |

**URL routing:**
- Global models → `https://aiplatform.googleapis.com/v1beta1/projects/{pid}/locations/global/publishers/google/models/{model}:streamGenerateContent`
- Regional models → `https://{location}-aiplatform.googleapis.com/v1beta1/projects/{pid}/locations/{location}/publishers/google/models/{model}:streamGenerateContent`

### 4.2 Routes Reference

| Route | Method | Purpose | Auth | Response |
|-------|--------|---------|------|----------|
| `/` | GET | Serve frontend | No | HTML (injects MODELS as JSON) |
| `/api/models` | GET | List all models | No | JSON dict |
| `/api/auth/google` | POST | Google Sign-In → JWT | No | `{token, user, hasKey, projectId}` |
| `/api/auth/dev` | POST | Dev-only login → JWT | No | `{token, user, hasKey, projectId}` |
| `/api/auth/verify` | POST | Verify existing JWT | JWT | `{valid, user, hasKey, projectId}` |
| `/api/save-key` | POST | Upload & validate SA key | JWT | `{ok, project_id, client_email}` |
| `/api/chat` | POST | Send message (streaming) | JWT | SSE stream |
| `/api/chats` | POST | List user's saved chats | JWT | JSON array |
| `/api/chats/save` | POST | Save/create chat | JWT | `{ok, id}` |
| `/api/chats/load` | POST | Load specific chat | JWT | Full chat JSON |
| `/api/chats/delete` | POST | Delete chat | JWT | `{ok}` |
| `/api/chats/rename` | POST | Rename chat topic | JWT | `{ok}` |
| `/api/pcm-to-wav` | POST | Convert PCM audio → WAV | No | `{data: base64_wav, mimeType}` |
| `/api/chats/save-media` | POST | Store media blob | JWT | `{ok, mediaId}` |
| `/api/chats/load-media` | POST | Retrieve media by ID | JWT | `{data, mimeType}` |

**Auth model:** JWT-based. Protected routes require `Authorization: Bearer <token>` header. Tokens are issued on login (Google or dev) with 24-hour expiry. The JWT payload contains `{email, name, picture}`. User data is keyed by email (SHA256 hash for local file paths, direct email for Postgres).

### 4.3 Authentication & Key Management

**Login flow:**
```
User → Google Sign-In (or dev bypass)
  → POST /api/auth/google (or /api/auth/dev)
  → Server verifies Google ID token (or accepts dev email)
  → Issues JWT (24h expiry) signed with JWT_SECRET
  → Frontend stores JWT in localStorage as 'tb-token'
  → All subsequent API calls include Authorization: Bearer <token>
```

**Service account key upload:**
```
User uploads SA key JSON → POST /api/save-key
  → Server parses JSON
  → Validates by calling get_access_token() (attempts token refresh)
  → Encrypts key with Fernet (ENCRYPTION_KEY) before storing
  → If valid: saves encrypted key to Postgres (or local_data/users/{hash}/sa_key.enc)
  → Also saves config with {project_id, client_email, updated_at}
```

**`get_access_token(sa_key_data)`:**
```python
creds = service_account.Credentials.from_service_account_info(
    sa_key_data, scopes=["https://www.googleapis.com/auth/cloud-platform"]
)
creds.refresh(google.auth.transport.requests.Request())
return creds.token
```

- Scoped to `cloud-platform` (full GCP access)
- Token is refreshed on **every chat request** (no caching)
- User directory: `SHA256(email.encode())[:16]` → e.g., `c7ffc1b57b8cba75`

### 4.4 Chat Endpoint (Core Logic)

**`POST /api/chat`** — the central endpoint. Full request body:

```json
{
  "email": "user@example.com",
  "model": "gemini-2.5-flash",
  "location": "us-central1",
  "messages": [{"role": "user", "parts": [{"text": "Hello"}]}],
  "generationConfig": {
    "temperature": 1.0,
    "topP": 0.95,
    "topK": null,
    "maxOutputTokens": 8192,
    "presencePenalty": 0,
    "frequencyPenalty": 0,
    "seed": null,
    "stopSequences": "STOP,END",
    "responseMimeType": "text/plain",
    "logprobs": null,
    "thinkingConfig": {"thinkingBudget": 8192},
    "responseModalities": null,
    "speechConfig": null,
    "audioTimestamp": false,
    "responseSchema": "{\"type\":\"object\",...}"
  },
  "googleSearch": true,
  "codeExecution": false,
  "systemInstruction": "You are a helpful assistant.",
  "ttsMode": false,
  "ttsVoice": "Kore",
  "imageGen": false
}
```

**Processing pipeline:**

1. **Validate** — email, model ID, messages presence, SA key existence
2. **Auto-detect mode** — if model's `group` is `"tts"` or `"image"`, force that mode regardless of frontend flags
3. **Get access token** — fresh OAuth2 token from SA key
4. **Build generationConfig** — cast each field to its expected type, silently drop invalid values
5. **Apply TTS overrides** (if ttsMode):
   - Force model to `gemini-2.5-flash-preview-tts` if not already a TTS model
   - Set `responseModalities: ["AUDIO"]`
   - Inject `speechConfig` with selected voice
   - **Clear system instruction** (TTS doesn't support it)
6. **Apply image gen overrides** (if imageGen):
   - Force model to `gemini-2.5-flash-image` if not already an image model
   - Set `responseModalities: ["TEXT", "IMAGE"]`
7. **Build tools** — only if NOT tts and NOT image mode:
   - `{"googleSearch": {}}` if search enabled
   - `{"codeExecution": {}}` if code exec enabled
8. **Build final body:**
   ```json
   {
     "contents": messages,
     "generationConfig": {...},
     "systemInstruction": {"parts": [{"text": "..."}]},
     "tools": [...]
   }
   ```
9. **Stream response** — POST to Vertex AI, parse chunked JSON, yield SSE events

### 4.5 Streaming JSON Parser

Vertex AI returns a JSON **array** of objects streamed in chunks: `[{...}, {...}, ...]`

The parser (inside `generate()`) handles this without a standard JSON streaming library:

```
Raw chunks → buffer accumulation → strip array brackets/commas
  → character-by-character brace matching (respecting strings & escapes)
  → extract complete JSON objects → json.loads() → yield as SSE
```

**Key details:**
- Tracks brace depth, string boundaries (`"`), and escape chars (`\`)
- Incomplete objects remain in buffer until more data arrives
- Failed `json.loads()` calls are silently skipped
- Stream ends with `data: [DONE]\n\n` sentinel

### 4.6 PCM-to-WAV Conversion

TTS models return raw PCM audio (`audio/L16;codec=pcm;rate=24000`). The `/api/pcm-to-wav` endpoint wraps it in a WAV container:

```python
# Accepts single base64 string OR array of base64 chunks
pcm = b"".join(base64.b64decode(chunk) for chunk in pcm_input)

# 44-byte WAV header (RIFF/WAVE/fmt/data)
wav_header = struct.pack('<4sI4s4sIHHIIHH4sI',
    b'RIFF', 36 + len(pcm), b'WAVE',
    b'fmt ', 16, 1, channels, sample_rate, byte_rate, block_align, bits,
    b'data', len(pcm))
```

Default parameters: 24000 Hz, mono, 16-bit PCM (matches Gemini TTS output).

### 4.7 Media Storage

Large media (images, audio) is stored **separately** from chat history to keep chat JSON small:

- **Save:** `POST /api/chats/save-media` → generates 12-char hex ID → writes `{data, mimeType}` as JSON file
- **Load:** `POST /api/chats/load-media` → reads by ID → returns `{data, mimeType}`
- **Location:** `local_data/users/{hash}/media/{mediaId}.json`
- **Reference in chat:** Messages have `_mediaRefs: [{type, mimeType, mediaId}]` linking to stored media

### 4.8 Path Traversal Protection

```python
def safe_id(raw_id):
    clean = re.sub(r'[^a-zA-Z0-9_-]', '', str(raw_id))
    return clean if clean else None
```

Applied to all user-supplied `chat_id` and `media_id` values before file path construction. Strips dots, slashes, and all non-alphanumeric characters except `-` and `_`. Returns `None` for empty results, which triggers error responses or new ID generation.

---

## 5. Frontend — index.html

Single-file SPA (≈1085 lines: HTML + CSS + JS). No build step. External deps: Google Sign-In SDK, marked.js (CDN), Google Sans font.

### 5.1 Global State

```javascript
const MODELS = {{ models | tojson }};     // Server-injected at render time
let user = null;                           // {name, email, picture}
let projectId = '';                        // GCP project ID
let region = 'us-central1';               // Selected region
let saKeyRaw = null;                       // Temp: raw SA key during upload
let messages = [];                         // Current chat: [{role, parts}, ...]
let totIn = 0, totOut = 0;                // Cumulative token counts
let currentChatId = null;                  // Active chat ID (null = new)
let chatList = [];                         // Sidebar chat entries
let attachments = [];                      // Pending file attachments [{name, mimeType, data}]
```

### 5.2 Auth Flow

```
Page load → check localStorage('tb-token' + 'tb-user')
  ├── Found → afterLogin()
  └── Not found → show login screen
       ├── Google Sign-In → POST /api/auth/google → JWT token → afterLogin()
       └── "Dev Login" (if DEV_LOGIN=true) → POST /api/auth/dev → JWT token → afterLogin()

afterLogin() → POST /api/auth/verify (with Bearer token)
  ├── valid + has_key → showApp() (main UI)
  ├── valid + no key → showKeyScreen() (SA key upload form)
  └── invalid/expired → clearAuth() → reload to login screen

Key upload → handleFile() → saveKey()
  → POST /api/save-key (validates + encrypts on server)
  → showApp()
```

**Persistence:** `localStorage` stores JWT as `tb-token`, user object as `tb-user`, and theme as `tb-theme`.

### 5.3 Toggle System & Mutual Exclusivity

Four toggles in the top bar: **Search**, **Code Exec**, **Image Gen**, **TTS**.

**Rules:**
- **TTS enabled** → Image Gen, Search, Code Exec all **disabled** (grayed out). Model auto-switches to first TTS model.
- **Image Gen enabled** → TTS, Search, Code Exec all **disabled**. Model auto-switches to first image model.
- **Disabling TTS/Image Gen** → all toggles re-enabled. If current model was TTS/image, switches back to `gemini-2.5-flash`.
- **Search + Code Exec** → can coexist freely with each other and chat models.

**When model dropdown changes** → `syncTogglesForModel()` reads the model's `group` and enforces the same rules.

**Backend enforcement:** The backend also auto-detects mode from model group (line 266-270) and excludes tools in TTS/image modes (line 366), so even if frontend toggles are wrong, behavior is correct.

### 5.4 Settings & Generation Config

**Settings panel** (`#settings-panel`) — collapsible grid with these sections:

**Generation Config:**
| Field | Element | Default | Type |
|-------|---------|---------|------|
| Temperature | `#s-temp` + `#s-temp-r` (dual) | 1.0 | float 0-2 |
| Max Output Tokens | `#s-max` + `#s-max-r` (dual) | 8192 | int 1-65536 |
| Top P | `#s-topp` + `#s-topp-r` (dual) | 0.95 | float 0-1 |
| Top K | `#s-topk` | blank (default) | int |
| Presence Penalty | `#s-pp` + `#s-pp-r` (dual) | 0 | float -2 to 2 |
| Frequency Penalty | `#s-fp` + `#s-fp-r` (dual) | 0 | float -2 to 2 |
| Seed | `#s-seed` | blank (random) | int |
| Stop Sequences | `#s-stop` | blank | comma-separated |
| Response MIME Type | `#s-mime` | text/plain | select |
| Logprobs | `#s-logp` | blank (off) | int |

**Thinking Config:**
| Field | Element | Default |
|-------|---------|---------|
| Enable thinking | `#s-think-on` | unchecked |
| Thinking budget | `#s-think-budget` | 8192 |

**TTS / Voice:**
| Field | Element | Default |
|-------|---------|---------|
| Voice | `#s-voice` | Kore |
| 14 voices available: Kore, Puck, Charon, Fenrir, Leda, Orus, Zephyr, Aoede, Achernar, Algenib, Schedar, Gacrux, Sulafat, Despina |

**Audio / Media:**
| Field | Element | Default |
|-------|---------|---------|
| Audio timestamps | `#s-audio-ts` | unchecked |

**JSON / Schema:**
| Field | Element |
|-------|---------|
| Response Schema | `#s-schema` (textarea, JSON) |

**System Instruction:**
| Field | Element |
|-------|---------|
| System prompt | `#sys-prompt` (textarea) |

**Dual inputs** (range + number) are bidirectionally synced via `oninput` handlers. The `setDual(id, val)` helper updates both.

**`getSettings()`** — collects all fields into a flat object for chat persistence.
**`getGenConfig()`** — subset of `getSettings()` containing only Vertex AI generation parameters.

### 5.5 Chat Flow (send → stream → render)

**`send()` function — the core message flow:**

```
1. Validate: need text or attachments, need user
2. Build parts: [{text}, {inlineData: {mimeType, data}}, ...]
3. Push user message to messages[]
4. Render user message bubble in DOM
5. Clear input, disable send button
6. Show "Thinking..." animation (pulsing dots)

7. POST /api/chat with full payload
8. If HTTP error → show error, revert message, re-enable send

9. Replace thinking animation with empty body div
   Create dedicated <span> for streaming text (textSpan)
   Show generation indicator for image/TTS modes

10. Read SSE stream via resp.body.getReader()
    For each line starting with "data: ":
      Parse JSON chunk
      For each part in chunk.candidates[0].content.parts:
        - text → append to textSpan.textContent (live update)
        - inlineData (image) → append <img> to body, track in mediaItems
        - inlineData (audio/PCM) → accumulate PCM chunks
        - inlineData (audio/other) → append <audio> to body
        - executableCode → append <pre><code> block
        - codeExecutionResult → append output div with green border
      Track usageMetadata and groundingMetadata

11. After stream ends:
    - If PCM chunks: batch convert to WAV via /api/pcm-to-wav
    - Strip [image]/[audio] placeholders from text
    - Re-render text as markdown (marked.js)
    - Re-append saved media elements
    - Add model message to messages[]

12. Build footer: copy buttons, search sources, token usage badge
13. Save media items to /api/chats/save-media, attach refs to message
14. Auto-save chat via saveCurrentChat()
15. Re-enable send button
```

**Key detail:** During streaming, text is written to a dedicated `<span>` element (`textSpan.textContent = full`) so that media elements (images, audio, code blocks) already appended to the body div are not destroyed. At stream end, the body is rebuilt with markdown-rendered text + preserved media elements.

### 5.6 Media Handling

**File attachments (upload):**
- Accepted: `image/*, audio/*, video/*, application/pdf`
- Max 50MB per file
- Read as base64 via FileReader
- Stored in `attachments[]` array, shown as thumbnails
- Sent as `{inlineData: {mimeType, data}}` parts in the message

**Media display (response):**
- **Images:** `<img>` with click-to-open-in-new-tab + download button
- **Audio (WAV/MP3):** `<audio controls>` + download button
- **Audio (PCM/L16):** Accumulated chunks → batch POST to `/api/pcm-to-wav` → `<audio controls>`
- **Code execution:** `<pre><code>` block + green-bordered output div

**Media persistence:**
- After streaming, each media item is POSTed to `/api/chats/save-media`
- Returns `mediaId`, stored as `message._mediaRefs`
- On chat load, `loadSavedMedia()` fetches each ref and renders

### 5.7 Chat History Management

**Sidebar** shows saved chats sorted by modification time (newest first).

| Action | Function | API Call |
|--------|----------|----------|
| List chats | `loadChatList()` | `POST /api/chats` |
| Load chat | `loadChat(id)` | `POST /api/chats/load` |
| Save chat | `saveCurrentChat()` | `POST /api/chats/save` |
| Delete chat | `deleteChat(id)` | `POST /api/chats/delete` |
| New chat | `newChat()` | (local reset only) |

**On load:** Restores messages, token counts, model selection, system prompt, and **all settings** (including toggles, penalties, schema, voice, thinking config).

**Auto-save:** Triggered after every completed model response.

**Topic generation:** `generateTopic()` uses first 8 words of first user message, or "Untitled".

### 5.8 Theme System

- Two themes: `light` (default) and `dark`
- Stored in `localStorage` as `tb-theme`
- Applied via `data-theme` attribute on `<html>`
- All colors use CSS custom properties (`--bg`, `--text`, `--accent`, etc.)
- Toggle button: ☀ (dark mode) / ☾ (light mode)

### 5.9 CSS Architecture

**Layout:** Flexbox-based. Body is horizontal flex (sidebar + main). Main is vertical flex (topbar + settings + chat + input).

**Responsive:** Single breakpoint at `max-width: 768px`:
- Sidebar becomes fixed overlay with slide-in animation
- Hamburger menu button appears
- Background overlay blocks interaction

**Component patterns:**
- `.ov` / `.card` — overlay screens (login, key setup)
- `.msg` / `.ma` / `.mc` / `.mb` / `.mf` — message structure
- `.tog` / `.slider` — custom toggle switches
- `.sf` / `.sg` / `.dual` — settings form layout
- `.btn` / `.btn-p` / `.sb-btn` — button variants
- `.media-wrap` / `.media-dl` — media containers

**Animations:**
- `.td span` — pulsing thinking dots (staggered 0.2s)
- `.spin` — rotating loading spinner
- `.tog .slider::after` — smooth toggle slide

---

## 6. Data Storage

### Production: Neon Postgres (via Vercel)

When `POSTGRES_URL` is set, all data is stored in Postgres. Tables are auto-created on startup:

| Table | Primary Key | Purpose |
|-------|------------|---------|
| `users` | `email` | User profiles, encrypted SA keys, config |
| `chats` | `(user_email, chat_id)` | Chat history, settings, token counts |
| `media` | `(user_email, media_id)` | Base64-encoded media blobs |
| `rate_limits` | `id` (serial) | Per-user rate limit tracking |

### Development: Local Files

When no `POSTGRES_URL` is set, falls back to file-based storage:

```
local_data/users/{sha256(email)[:16]}/
```

### Service Account Key (`sa_key.enc`)

Encrypted with Fernet (`ENCRYPTION_KEY`). Falls back to base64 encoding if no encryption key is set.

### Config (stored in `users` table or `config.json`)

```json
{
  "project_id": "my-gcp-project",
  "client_email": "sa@project.iam.gserviceaccount.com",
  "updated_at": 1775131170.355
}
```

### Chat File (`chats/{id}.json`)

```json
{
  "id": "a1b2c3d4",
  "topic": "First 8 words of first message...",
  "model": "gemini-2.5-flash",
  "messages": [
    {
      "role": "user",
      "parts": [{"text": "Hello"}, {"inlineData": {"mimeType": "image/png", "data": "base64..."}}]
    },
    {
      "role": "model",
      "parts": [{"text": "Hi there! [image]"}],
      "_mediaRefs": [{"type": "image", "mimeType": "image/png", "mediaId": "abc123def456"}]
    }
  ],
  "settings": {
    "temperature": 1.0,
    "maxOutputTokens": 8192,
    "topP": 0.95,
    "googleSearch": true,
    "codeExecution": false,
    "imageGen": false,
    "ttsMode": false,
    "ttsVoice": "Kore",
    "thinkingConfig": {"thinkingBudget": 8192}
  },
  "systemPrompt": "You are a helpful assistant.",
  "updated": 1775131170.355,
  "totIn": 1500,
  "totOut": 800
}
```

### Media File (`media/{id}.json`)

```json
{
  "data": "base64_encoded_binary_data",
  "mimeType": "image/png"
}
```

---

## 7. API Contract Reference

### POST `/api/chat` — Full Request

**Header:** `Authorization: Bearer <JWT token>`

```json
{
  "model": "string (must be in MODELS registry)",
  "location": "string (default: us-central1, used for regional models)",
  "messages": [
    {"role": "user|model", "parts": [{"text": "..."}, {"inlineData": {"mimeType": "...", "data": "base64"}}]}
  ],
  "generationConfig": {
    "temperature": "float 0-2",
    "topP": "float 0-1",
    "topK": "int",
    "maxOutputTokens": "int",
    "candidateCount": "int",
    "presencePenalty": "float -2 to 2",
    "frequencyPenalty": "float -2 to 2",
    "seed": "int",
    "responseMimeType": "text/plain | application/json",
    "logprobs": "int",
    "stopSequences": "string (comma-sep) or array",
    "thinkingConfig": {"thinkingBudget": "int"},
    "responseModalities": ["TEXT", "IMAGE", "AUDIO"],
    "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": "Kore"}}},
    "audioTimestamp": "bool",
    "responseSchema": "JSON string or object"
  },
  "googleSearch": "bool",
  "codeExecution": "bool",
  "systemInstruction": "string",
  "ttsMode": "bool",
  "ttsVoice": "string",
  "imageGen": "bool"
}
```

### SSE Response Format

```
data: {"candidates":[{"content":{"role":"model","parts":[{"text":"Hello"}]}}],"usageMetadata":{...},"modelVersion":"gemini-2.5-flash"}

data: {"candidates":[{"content":{"parts":[{"inlineData":{"mimeType":"image/png","data":"base64..."}}]}}]}

data: {"candidates":[{"content":{"parts":[{"executableCode":{"code":"print('hi')"}}]}}]}

data: {"candidates":[{"content":{"parts":[{"codeExecutionResult":{"output":"hi\n"}}]}}]}

data: [DONE]
```

**Usage metadata fields:** `promptTokenCount`, `candidatesTokenCount`, `totalTokenCount`, `thoughtsTokenCount`

**Grounding metadata:** `candidates[0].groundingMetadata.groundingChunks[].web.{uri, title, domain}`

---

## 8. Dependencies

| Package | Version | Purpose |
|---------|---------|---------|
| `flask` | ≥3.0 | Web framework, template rendering, request handling |
| `google-auth` | ≥2.0 | Service account credential management, OAuth2 token refresh, ID token verification |
| `google-cloud-aiplatform` | ≥1.60 | Vertex AI SDK (transitive dep for `google.auth`) |
| `requests` | ≥2.31 | HTTP client for Vertex AI API calls |
| `psycopg2-binary` | ≥2.9 | PostgreSQL driver (Neon Postgres) |
| `PyJWT` | ≥2.8 | JWT token encoding/decoding for session auth |
| `cryptography` | ≥42.0 | Fernet encryption for service account keys at rest |

**Frontend CDN deps:**
- `https://accounts.google.com/gsi/client` — Google Identity Services (Sign-In)
- `https://cdn.jsdelivr.net/npm/marked/marked.min.js` — Markdown parser
- Google Sans font (Google Fonts CDN)

**Note:** `google-cloud-aiplatform` is listed in requirements but the app uses raw REST API calls via `requests` rather than the SDK client. It's needed for `google.auth` transitive dependency.

---

## 9. How to Run

### Prerequisites

- Python 3.8+
- GCP project with Vertex AI API enabled
- Service account JSON key with **Vertex AI User** role

### Local Development Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Start with dev login enabled (no Google OAuth needed)
JWT_SECRET="dev-secret" DEV_LOGIN=true python app.py
```

**Environment variables:**

| Variable | Default | Description |
|----------|---------|-------------|
| `POSTGRES_URL` | (empty) | Neon Postgres connection string. If unset, uses local file storage |
| `JWT_SECRET` | `dev-secret-change-in-prod` | Secret for signing JWT tokens |
| `ENCRYPTION_KEY` | (empty) | Fernet key for encrypting SA keys at rest |
| `GOOGLE_CLIENT_ID` | (empty) | OAuth client ID for Google Sign-In |
| `ALLOWED_ORIGINS` | `http://localhost:5000` | Comma-separated CORS origins |
| `DEV_LOGIN` | `true` | Enable dev login without Google (set `false` in production) |
| `DATA_DIR` | `local_data` | Where local file storage is kept (dev only) |

**Server starts at:** `http://127.0.0.1:5000`

> **Note:** macOS uses port 5000 for AirPlay Receiver. If you get a 403, start on another port or disable AirPlay Receiver in System Settings → General → AirDrop & Handoff.

### First Use

1. Open `http://localhost:5000`
2. Click **Dev Login** (or use Google Sign-In if `GOOGLE_CLIENT_ID` is set)
3. Upload your GCP service account JSON key
4. Select a model and start chatting

### Production Deployment

See [DEPLOY.md](DEPLOY.md) for the full Vercel + Neon Postgres deployment guide.

---

## 10. Known Quirks & Edge Cases

### Backend

1. **TTS clears system instruction** — when TTS mode is active, `system_instruction` is set to `""` (line 359). The original value is not preserved or restored.

2. **Token not cached** — `get_access_token()` is called fresh for every `/api/chat` request. No caching layer. Works but adds latency.

3. **Streaming parser silently drops bad JSON** — if a chunk produces invalid JSON, it's skipped with `continue`. No error sent to client.

4. **Delete non-existent chat returns 200** — `path.unlink()` only runs if file exists; otherwise silently succeeds. (Rename returns 404 for missing chats.)

5. **`safe_id` on save generates new UUID** — if the provided chat ID sanitizes to empty, a new UUID is generated rather than returning an error.

6. **`candidateCount` in config** — parsed but rarely useful. Vertex AI typically returns 1 candidate for streaming.

7. **Model auto-switch checks substring** — TTS override triggers if `"tts" not in model_id`, image if `"image" not in model_id`. A hypothetical model named `"my-image-chat"` would bypass the override.

### Frontend

8. **Region not persisted** — the region selected during key setup sets a JS variable but is not saved to localStorage. Resets to `us-central1` on page reload. The topbar region selector is independent and also not persisted.

9. **Settings overwritten on chat load** — loading any chat replaces ALL current settings with that chat's saved settings. No "global defaults" concept.

10. **Auto-scroll always on** — `ca.scrollTop = ca.scrollHeight` runs after every streaming chunk. If user scrolled up to read earlier messages, they'll be yanked to the bottom.

11. **Model links in markdown don't open in new tab** — links rendered by `marked.js` in model responses use default `<a>` tags (same-tab navigation). Only grounding source links have `target="_blank"`.

12. **`[image]`/`[audio]` in stored messages** — model messages store placeholder text like `"Here is the image [image]"`. These are stripped on display but exist in the raw data. If a future change removes the stripping, they'll be visible.

13. **PCM chunk ordering** — chunks are concatenated in arrival order. If network issues cause out-of-order delivery, audio will be corrupted. (Unlikely with HTTP streaming but theoretically possible.)

14. **No global settings persistence** — each chat has its own settings snapshot. There's no way to set persistent defaults across new chats.

---

## 11. Git History & Evolution

```
1039558  feat: add input sanitization for file operations and introduce initial frontend template
e5842c6  feat: add frontend UI template for GCP Token Bench dashboard
9e351d4  feat: add support for TTS and image generation models, media handling endpoints, and a new frontend interface
edbd879  feat: add UI template and configure 50MB request limit in Flask app
c409e89  feat: add frontend UI with chat interface and settings panel
e1551e7  feat: implement persistent chat history management with CRUD API endpoints and index template
a5cbf25  feat: implement frontend UI and service account configuration for GCP model benchmarking
262fad6  feat: add frontend UI for GCP project configuration and model interaction
c6a10ff  refactor: migrate backend from Node.js to Flask and reorganize project structure
32236a9  Fix API route and add local dev server
516a395  Add service account key files to .gitignore
5285582  Use stable Gemini model aliases instead of preview version IDs
c7f60e1  Switch to Vertex AI with service account JSON key auth
b6a360f  Remove .claude settings from repo and add to .gitignore
489e3a5  Add Google Sign-In via server config and Vercel API route
1a26d5e  Initial commit: GCP Token Bench
```

**Key milestones:**
- Started as Node.js app, migrated to Flask (`c6a10ff`)
- Progressive frontend: config UI → chat → settings panel → TTS/image support
- Security hardening: `.gitignore` for keys, input sanitization (`1039558`)

---

## 12. Security Considerations

### Currently Protected

- **JWT authentication** — all protected routes require valid Bearer token with 24-hour expiry
- **SA key encryption** — keys encrypted with Fernet (AES-128-CBC) before storage when `ENCRYPTION_KEY` is set
- **Path traversal** — `safe_id()` strips all non-alphanumeric characters from user-supplied IDs
- **SA key validation** — keys are validated (token refresh attempted) before being stored
- **Rate limiting** — `/api/chat` limited to 30 requests/min per user (Postgres only)
- **CORS** — only allows requests from configured `ALLOWED_ORIGINS`
- **CSP headers** — Content-Security-Policy restricts script/style/connect sources
- **Sensitive files gitignored** — `api_key.json`, `*-key.json`, `*-credentials.json`, `local_data/`
- **XSS in user info** — user name/picture escaped via `esc()` in sidebar rendering
- **XSS in grounding links** — URI and title escaped, `rel="noopener"` added
- **Request size limit** — 50MB max (`MAX_CONTENT_LENGTH`)

### Considerations for Production

- **No CSRF protection** — POST endpoints rely on JWT Bearer tokens (not cookies), which mitigates most CSRF. Consider CSRF tokens for additional defense.
- **Dev login must be disabled** — set `DEV_LOGIN=false` in production. Otherwise anyone can log in without Google auth.
- **JWT secret must be changed** — the default `dev-secret-change-in-prod` must be replaced with a strong random string.
- **No HTTPS** — development server runs HTTP. Vercel provides HTTPS automatically in production.
- **Rate limiting scope** — only `/api/chat` is rate-limited. Key upload, media ops, and chat CRUD are unlimited.
