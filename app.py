import json
import os
import re
import hashlib
import time
import uuid
import struct
import base64
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import Flask, render_template, request, Response, stream_with_context, jsonify
from google.oauth2 import service_account, id_token as google_id_token
from google.auth.transport import requests as google_transport
import google.auth.transport.requests
import requests as req_lib
import jwt as pyjwt
from cryptography.fernet import Fernet

# ── Config ──────────────────────────────────────────────────────────────────

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024

MONGODB_URI = os.environ.get("MONGODB_URI", "")
JWT_SECRET = os.environ.get("JWT_SECRET", os.environ.get("FLASK_SECRET", "dev-secret-change-in-prod"))
ENCRYPTION_KEY = os.environ.get("ENCRYPTION_KEY", "")
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
ALLOWED_ORIGINS = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "http://localhost:5000").split(",") if o.strip()]
DEV_LOGIN = os.environ.get("DEV_LOGIN", "true").lower() == "true"

# ── MongoDB ─────────────────────────────────────────────────────────────────

db = None
if MONGODB_URI:
    from pymongo import MongoClient
    _client = MongoClient(MONGODB_URI, maxPoolSize=10, serverSelectionTimeoutMS=5000, connectTimeoutMS=5000)
    db = _client.get_default_database() if "/" in MONGODB_URI.split("?")[0].split("//")[-1] else _client["tokenbench"]
    db.users.create_index("email", unique=True)
    db.chats.create_index([("user_email", 1), ("chat_id", 1)], unique=True)
    db.chats.create_index([("user_email", 1), ("updated_at", -1)])
    db.media.create_index([("user_email", 1), ("media_id", 1)], unique=True)
    db.rate_limits.create_index("expires_at", expireAfterSeconds=0)

# ── Encryption ──────────────────────────────────────────────────────────────

_fernet = Fernet(ENCRYPTION_KEY.encode()) if ENCRYPTION_KEY else None


def encrypt_sa_key(key_data):
    raw = json.dumps(key_data).encode()
    if _fernet:
        return _fernet.encrypt(raw).decode()
    return base64.b64encode(raw).decode()


def decrypt_sa_key(encrypted):
    if _fernet:
        return json.loads(_fernet.decrypt(encrypted.encode()))
    return json.loads(base64.b64decode(encrypted.encode()))


# ── JWT Auth ────────────────────────────────────────────────────────────────

def create_token(email, name="", picture=""):
    payload = {
        "email": email, "name": name, "picture": picture,
        "iat": datetime.now(timezone.utc),
        "exp": datetime.now(timezone.utc) + timedelta(hours=24),
    }
    return pyjwt.encode(payload, JWT_SECRET, algorithm="HS256")


def decode_token(token):
    return pyjwt.decode(token, JWT_SECRET, algorithms=["HS256"])


def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        header = request.headers.get("Authorization", "")
        token = header.removeprefix("Bearer ").strip() if header.startswith("Bearer ") else ""
        if not token:
            return jsonify({"error": "Authentication required"}), 401
        try:
            payload = decode_token(token)
        except pyjwt.ExpiredSignatureError:
            return jsonify({"error": "Token expired"}), 401
        except pyjwt.InvalidTokenError:
            return jsonify({"error": "Invalid token"}), 401
        request.user_email = payload["email"]
        request.user_name = payload.get("name", "")
        return f(*args, **kwargs)
    return decorated


# ── Security Middleware ─────────────────────────────────────────────────────

@app.after_request
def security_headers(response):
    origin = request.headers.get("Origin", "")
    if origin in ALLOWED_ORIGINS:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Max-Age"] = "3600"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "0"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    csp = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://accounts.google.com https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com; "
        "connect-src 'self' https://accounts.google.com; "
        "frame-src https://accounts.google.com; "
        "img-src 'self' data: https://*.googleusercontent.com; "
        "media-src 'self' data: blob:;"
    )
    response.headers["Content-Security-Policy"] = csp
    return response


@app.before_request
def handle_preflight():
    if request.method == "OPTIONS":
        resp = app.make_default_options_response()
        origin = request.headers.get("Origin", "")
        if origin in ALLOWED_ORIGINS:
            resp.headers["Access-Control-Allow-Origin"] = origin
            resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
            resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        return resp


def check_rate_limit(email, endpoint, limit=60, window=60):
    if not db:
        return True
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=window)
    count = db.rate_limits.count_documents({"email": email, "endpoint": endpoint, "timestamp": {"$gte": cutoff}})
    if count >= limit:
        return False
    db.rate_limits.insert_one({"email": email, "endpoint": endpoint, "timestamp": now, "expires_at": now + timedelta(seconds=window)})
    return True


# ── Helpers ─────────────────────────────────────────────────────────────────

def safe_id(raw_id):
    clean = re.sub(r'[^a-zA-Z0-9_-]', '', str(raw_id))
    return clean if clean else None


def get_access_token(sa_key_data):
    creds = service_account.Credentials.from_service_account_info(sa_key_data, scopes=["https://www.googleapis.com/auth/cloud-platform"])
    creds.refresh(google.auth.transport.requests.Request())
    return creds.token


# ── Storage Abstraction ─────────────────────────────────────────────────────
# Uses MongoDB when configured, falls back to local files for dev.

from pathlib import Path
DATA_DIR = Path(os.environ.get("DATA_DIR", "local_data"))


def _user_dir(email):
    h = hashlib.sha256(email.encode()).hexdigest()[:16]
    d = DATA_DIR / "users" / h
    d.mkdir(parents=True, exist_ok=True)
    return d


def _chats_dir(email):
    d = _user_dir(email) / "chats"
    d.mkdir(exist_ok=True)
    return d


def save_user_sa_key(email, key_data):
    encrypted = encrypt_sa_key(key_data)
    config = {"project_id": key_data.get("project_id", ""), "client_email": key_data.get("client_email", ""), "updated_at": time.time()}
    if db:
        db.users.update_one({"email": email}, {"$set": {"sa_key_encrypted": encrypted, "config": config, "updated_at": datetime.now(timezone.utc)}}, upsert=True)
    else:
        d = _user_dir(email)
        with open(d / "sa_key.enc", "w") as f:
            f.write(encrypted)
        with open(d / "config.json", "w") as f:
            json.dump(config, f)


def load_user_sa_key(email):
    if db:
        user = db.users.find_one({"email": email}, {"sa_key_encrypted": 1, "config": 1})
        if user and user.get("sa_key_encrypted"):
            return decrypt_sa_key(user["sa_key_encrypted"]), user.get("config", {})
        return None, None
    else:
        d = _user_dir(email)
        enc_path = d / "sa_key.enc"
        legacy_path = d / "sa_key.json"
        if enc_path.exists():
            with open(enc_path) as f:
                return decrypt_sa_key(f.read()), _load_config_file(d)
        if legacy_path.exists():
            with open(legacy_path) as f:
                return json.load(f), _load_config_file(d)
        return None, None


def _load_config_file(d):
    config_path = d / "config.json"
    if config_path.exists():
        with open(config_path) as f:
            return json.load(f)
    return {}


def has_user_key(email):
    if db:
        user = db.users.find_one({"email": email}, {"config": 1, "sa_key_encrypted": 1})
        if user and user.get("sa_key_encrypted"):
            return True, user.get("config", {})
        return False, {}
    else:
        _, config = load_user_sa_key(email)
        return config is not None, config or {}


def list_chats(email):
    if db:
        cursor = db.chats.find({"user_email": email}, {"chat_id": 1, "topic": 1, "model": 1, "updated_at": 1, "messages": {"$slice": 0}}).sort("updated_at", -1)
        result = []
        for c in cursor:
            result.append({"id": c["chat_id"], "topic": c.get("topic", "Untitled"), "model": c.get("model", ""), "updated": c.get("updated_at", datetime.now(timezone.utc)).timestamp(), "messageCount": c.get("message_count", 0)})
        return result
    else:
        d = _chats_dir(email)
        chats = []
        for f in sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            with open(f) as fp:
                meta = json.load(fp)
            chats.append({"id": f.stem, "topic": meta.get("topic", "Untitled"), "model": meta.get("model", ""), "updated": meta.get("updated", 0), "messageCount": len(meta.get("messages", []))})
        return chats


def save_chat(email, chat_id, data):
    if db:
        doc = {
            "user_email": email, "chat_id": chat_id,
            "topic": data.get("topic", "Untitled"), "model": data.get("model", ""),
            "messages": data.get("messages", []), "settings": data.get("settings", {}),
            "system_prompt": data.get("systemPrompt", ""),
            "tot_in": data.get("totIn", 0), "tot_out": data.get("totOut", 0),
            "message_count": len(data.get("messages", [])),
            "updated_at": datetime.now(timezone.utc),
        }
        db.chats.update_one({"user_email": email, "chat_id": chat_id}, {"$set": doc, "$setOnInsert": {"created_at": datetime.now(timezone.utc)}}, upsert=True)
    else:
        d = _chats_dir(email)
        chat_data = {
            "id": chat_id, "topic": data.get("topic", "Untitled"), "model": data.get("model", ""),
            "messages": data.get("messages", []), "settings": data.get("settings", {}),
            "systemPrompt": data.get("systemPrompt", ""), "updated": time.time(),
            "totIn": data.get("totIn", 0), "totOut": data.get("totOut", 0),
        }
        with open(d / f"{chat_id}.json", "w") as f:
            json.dump(chat_data, f)


def load_chat(email, chat_id):
    if db:
        c = db.chats.find_one({"user_email": email, "chat_id": chat_id})
        if not c:
            return None
        return {
            "id": c["chat_id"], "topic": c.get("topic", "Untitled"), "model": c.get("model", ""),
            "messages": c.get("messages", []), "settings": c.get("settings", {}),
            "systemPrompt": c.get("system_prompt", ""), "updated": c.get("updated_at", datetime.now(timezone.utc)).timestamp(),
            "totIn": c.get("tot_in", 0), "totOut": c.get("tot_out", 0),
        }
    else:
        path = _chats_dir(email) / f"{chat_id}.json"
        if not path.exists():
            return None
        with open(path) as f:
            return json.load(f)


def delete_chat(email, chat_id):
    if db:
        db.chats.delete_one({"user_email": email, "chat_id": chat_id})
    else:
        path = _chats_dir(email) / f"{chat_id}.json"
        if path.exists():
            path.unlink()


def rename_chat(email, chat_id, topic):
    if db:
        result = db.chats.update_one({"user_email": email, "chat_id": chat_id}, {"$set": {"topic": topic}})
        return result.matched_count > 0
    else:
        path = _chats_dir(email) / f"{chat_id}.json"
        if not path.exists():
            return False
        with open(path) as f:
            chat = json.load(f)
        chat["topic"] = topic
        with open(path, "w") as f:
            json.dump(chat, f)
        return True


def save_media(email, media_id, data, mime_type):
    if db:
        db.media.update_one(
            {"user_email": email, "media_id": media_id},
            {"$set": {"data": data, "mime_type": mime_type, "created_at": datetime.now(timezone.utc)}},
            upsert=True,
        )
    else:
        media_dir = _user_dir(email) / "media"
        media_dir.mkdir(exist_ok=True)
        with open(media_dir / f"{media_id}.json", "w") as f:
            json.dump({"data": data, "mimeType": mime_type}, f)


def load_media(email, media_id):
    if db:
        m = db.media.find_one({"user_email": email, "media_id": media_id})
        if not m:
            return None
        return {"data": m["data"], "mimeType": m["mime_type"]}
    else:
        path = _user_dir(email) / "media" / f"{media_id}.json"
        if not path.exists():
            return None
        with open(path) as f:
            return json.load(f)


# ── Models ──────────────────────────────────────────────────────────────────

MODELS = {
    "gemini-3.1-pro-preview": {
        "name": "Gemini 3.1 Pro (Preview)", "input_price": 2.00, "output_price": 12.00,
        "context": 1048576, "category": "Best reasoning", "global": True,
    },
    "gemini-3-flash-preview": {
        "name": "Gemini 3 Flash (Preview)", "input_price": 0.50, "output_price": 3.00,
        "context": 1048576, "category": "Fast + smart", "global": True,
    },
    "gemini-3.1-flash-lite-preview": {
        "name": "Gemini 3.1 Flash Lite (Preview)", "input_price": 0.25, "output_price": 1.50,
        "context": 1048576, "category": "Cheapest 3.x", "global": True,
    },
    "gemini-2.5-pro": {
        "name": "Gemini 2.5 Pro", "input_price": 1.25, "output_price": 10.00,
        "context": 1048576, "category": "Stable reasoning", "global": False,
    },
    "gemini-2.5-flash": {
        "name": "Gemini 2.5 Flash", "input_price": 0.30, "output_price": 2.50,
        "context": 1048576, "category": "Best value", "global": False,
    },
    "gemini-2.5-flash-lite": {
        "name": "Gemini 2.5 Flash Lite", "input_price": 0.10, "output_price": 0.40,
        "context": 1048576, "category": "Cheapest", "global": False,
    },
    "gemini-2.5-flash-preview-tts": {
        "name": "Gemini 2.5 Flash TTS", "input_price": 0.30, "output_price": 2.50,
        "context": 32768, "category": "Text-to-Speech", "global": False, "group": "tts",
    },
    "gemini-2.5-pro-preview-tts": {
        "name": "Gemini 2.5 Pro TTS", "input_price": 1.25, "output_price": 10.00,
        "context": 32768, "category": "Text-to-Speech (HD)", "global": False, "group": "tts",
    },
    "gemini-2.5-flash-image": {
        "name": "Gemini 2.5 Flash Image", "input_price": 0.30, "output_price": 2.50,
        "context": 1048576, "category": "Image Generation", "global": False, "group": "image",
    },
    "gemini-3.1-flash-image-preview": {
        "name": "Gemini 3.1 Flash Image (Preview)", "input_price": 0.50, "output_price": 3.00,
        "context": 1048576, "category": "Image Gen (3.x)", "global": True, "group": "image",
    },
    "gemini-3-pro-image-preview": {
        "name": "Gemini 3 Pro Image (Preview)", "input_price": 2.00, "output_price": 12.00,
        "context": 1048576, "category": "Image Gen (HD)", "global": True, "group": "image",
    },
}

REGIONAL_URL = "https://{location}-aiplatform.googleapis.com/v1beta1/projects/{project_id}/locations/{location}/publishers/google/models/{model}:streamGenerateContent"
GLOBAL_URL = "https://aiplatform.googleapis.com/v1beta1/projects/{project_id}/locations/global/publishers/google/models/{model}:streamGenerateContent"


# ── Auth Routes ─────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html", models=MODELS, google_client_id=GOOGLE_CLIENT_ID, dev_login=DEV_LOGIN)


@app.route("/api/models")
def get_models():
    return jsonify(MODELS)


@app.route("/api/auth/google", methods=["POST"])
def auth_google():
    """Verify a Google ID token and return a session JWT."""
    data = request.json or {}
    credential = data.get("credential", "")
    if not credential:
        return jsonify({"error": "Missing credential"}), 400
    if not GOOGLE_CLIENT_ID:
        return jsonify({"error": "Google Sign-In not configured"}), 400
    try:
        idinfo = google_id_token.verify_oauth2_token(credential, google_transport.Request(), GOOGLE_CLIENT_ID)
        if idinfo.get("iss") not in ("accounts.google.com", "https://accounts.google.com"):
            return jsonify({"error": "Invalid issuer"}), 401
    except ValueError as e:
        return jsonify({"error": f"Invalid token: {e}"}), 401
    email = idinfo["email"]
    name = idinfo.get("name", "")
    picture = idinfo.get("picture", "")
    if db:
        db.users.update_one({"email": email}, {"$set": {"name": name, "picture": picture, "last_login": datetime.now(timezone.utc)}, "$setOnInsert": {"created_at": datetime.now(timezone.utc)}}, upsert=True)
    token = create_token(email, name, picture)
    has_key, config = has_user_key(email)
    return jsonify({"token": token, "user": {"name": name, "email": email, "picture": picture}, "hasKey": has_key, "projectId": config.get("project_id", ""), "clientEmail": config.get("client_email", "")})


@app.route("/api/auth/dev", methods=["POST"])
def auth_dev():
    """Dev-only login without Google (disabled in production)."""
    if not DEV_LOGIN:
        return jsonify({"error": "Dev login disabled"}), 403
    data = request.json or {}
    email = data.get("email", "dev@localhost").strip()
    if not email or not re.match(r'^[^@\s]+@[^@\s]+$', email):
        return jsonify({"error": "Invalid email"}), 400
    name = data.get("name", "Local Dev")
    if db:
        db.users.update_one({"email": email}, {"$set": {"name": name, "last_login": datetime.now(timezone.utc)}, "$setOnInsert": {"created_at": datetime.now(timezone.utc)}}, upsert=True)
    token = create_token(email, name, "")
    has_key, config = has_user_key(email)
    return jsonify({"token": token, "user": {"name": name, "email": email, "picture": ""}, "hasKey": has_key, "projectId": config.get("project_id", ""), "clientEmail": config.get("client_email", "")})


@app.route("/api/auth/verify", methods=["POST"])
@require_auth
def auth_verify():
    """Verify an existing JWT and return current user state."""
    email = request.user_email
    has_key, config = has_user_key(email)
    return jsonify({"valid": True, "user": {"email": email, "name": request.user_name}, "hasKey": has_key, "projectId": config.get("project_id", ""), "clientEmail": config.get("client_email", "")})


# ── Key Management ──────────────────────────────────────────────────────────

@app.route("/api/save-key", methods=["POST"])
@require_auth
def save_key():
    data = request.json
    sa_key_raw = data.get("serviceAccountKey", "")
    if not sa_key_raw:
        return jsonify({"error": "Missing key"}), 400
    try:
        sa_key = json.loads(sa_key_raw) if isinstance(sa_key_raw, str) else sa_key_raw
    except json.JSONDecodeError:
        return jsonify({"error": "Invalid JSON key"}), 400
    try:
        get_access_token(sa_key)
    except Exception as e:
        return jsonify({"error": f"Key validation failed: {e}"}), 400
    save_user_sa_key(request.user_email, sa_key)
    return jsonify({"ok": True, "project_id": sa_key.get("project_id", ""), "client_email": sa_key.get("client_email", "")})


# ── Chat History ────────────────────────────────────────────────────────────

@app.route("/api/chats", methods=["POST"])
@require_auth
def api_list_chats():
    return jsonify(list_chats(request.user_email))


@app.route("/api/chats/save", methods=["POST"])
@require_auth
def api_save_chat():
    data = request.json
    chat_id = safe_id(data.get("id")) or str(uuid.uuid4())[:8]
    save_chat(request.user_email, chat_id, data)
    return jsonify({"ok": True, "id": chat_id})


@app.route("/api/chats/load", methods=["POST"])
@require_auth
def api_load_chat():
    chat_id = safe_id((request.json or {}).get("id", ""))
    if not chat_id:
        return jsonify({"error": "Missing params"}), 400
    result = load_chat(request.user_email, chat_id)
    if not result:
        return jsonify({"error": "Chat not found"}), 404
    return jsonify(result)


@app.route("/api/chats/delete", methods=["POST"])
@require_auth
def api_delete_chat():
    chat_id = safe_id((request.json or {}).get("id", ""))
    if not chat_id:
        return jsonify({"error": "Missing params"}), 400
    delete_chat(request.user_email, chat_id)
    return jsonify({"ok": True})


@app.route("/api/chats/rename", methods=["POST"])
@require_auth
def api_rename_chat():
    data = request.json or {}
    chat_id = safe_id(data.get("id", ""))
    topic = data.get("topic", "")
    if not chat_id:
        return jsonify({"error": "Missing params"}), 400
    if not rename_chat(request.user_email, chat_id, topic):
        return jsonify({"error": "Not found"}), 404
    return jsonify({"ok": True})


# ── Chat API ────────────────────────────────────────────────────────────────

@app.route("/api/chat", methods=["POST"])
@require_auth
def chat():
    email = request.user_email
    if not check_rate_limit(email, "chat", limit=30, window=60):
        return jsonify({"error": "Rate limit exceeded. Try again in a minute."}), 429

    data = request.json
    model_id = data.get("model", "gemini-2.5-flash")
    location = data.get("location", "us-central1")
    messages = data.get("messages", [])
    gen_config = data.get("generationConfig", {})
    use_search = data.get("googleSearch", False)
    use_code_exec = data.get("codeExecution", False)
    system_instruction = data.get("systemInstruction", "")
    tts_mode = data.get("ttsMode", False)
    tts_voice = data.get("ttsVoice", "Kore")
    image_gen = data.get("imageGen", False)

    model_group = MODELS.get(model_id, {}).get("group", "chat")
    if model_group == "tts":
        tts_mode = True
    elif model_group == "image":
        image_gen = True

    if model_id not in MODELS:
        return jsonify({"error": f"Unknown model: {model_id}"}), 400
    if not messages:
        return jsonify({"error": "No messages"}), 400

    sa_key, _ = load_user_sa_key(email)
    if not sa_key:
        return jsonify({"error": "No service account key found."}), 400

    try:
        access_token = get_access_token(sa_key)
    except Exception as e:
        return jsonify({"error": f"Auth failed: {e}"}), 401

    project_id = sa_key.get("project_id", "")
    model_info = MODELS[model_id]
    if model_info.get("global"):
        url = GLOBAL_URL.format(project_id=project_id, model=model_id)
    else:
        url = REGIONAL_URL.format(location=location, project_id=project_id, model=model_id)

    # Build generationConfig
    final_config = {}
    config_fields = {
        "temperature": float, "topP": float, "topK": int,
        "maxOutputTokens": int, "candidateCount": int,
        "presencePenalty": float, "frequencyPenalty": float,
        "seed": int, "responseMimeType": str, "logprobs": int,
    }
    for key, cast in config_fields.items():
        if key in gen_config and gen_config[key] is not None and gen_config[key] != "":
            try:
                final_config[key] = cast(gen_config[key])
            except (ValueError, TypeError):
                pass

    if "stopSequences" in gen_config and gen_config["stopSequences"]:
        seqs = gen_config["stopSequences"]
        if isinstance(seqs, str):
            seqs = [s.strip() for s in seqs.split(",") if s.strip()]
        final_config["stopSequences"] = seqs

    if "thinkingConfig" in gen_config and gen_config["thinkingConfig"]:
        final_config["thinkingConfig"] = gen_config["thinkingConfig"]
    if "responseModalities" in gen_config and gen_config["responseModalities"]:
        final_config["responseModalities"] = gen_config["responseModalities"]
    if "speechConfig" in gen_config and gen_config["speechConfig"]:
        final_config["speechConfig"] = gen_config["speechConfig"]
    if "audioTimestamp" in gen_config:
        final_config["audioTimestamp"] = bool(gen_config["audioTimestamp"])

    if "responseSchema" in gen_config and gen_config["responseSchema"]:
        try:
            schema = gen_config["responseSchema"]
            if isinstance(schema, str):
                schema = json.loads(schema)
            final_config["responseSchema"] = schema
        except (json.JSONDecodeError, TypeError):
            pass

    # TTS mode overrides
    if tts_mode:
        if "tts" not in model_id:
            model_id = "gemini-2.5-flash-preview-tts"
            model_info = MODELS[model_id]
            url = REGIONAL_URL.format(location=location, project_id=project_id, model=model_id)
        final_config["responseModalities"] = ["AUDIO"]
        final_config["speechConfig"] = {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": tts_voice}}}
        system_instruction = ""

    # Image generation mode
    if image_gen:
        if "image" not in model_id:
            model_id = "gemini-2.5-flash-image"
            model_info = MODELS[model_id]
            url = REGIONAL_URL.format(location=location, project_id=project_id, model=model_id)
        final_config["responseModalities"] = ["TEXT", "IMAGE"]

    body = {"contents": messages, "generationConfig": final_config}
    if system_instruction:
        body["systemInstruction"] = {"parts": [{"text": system_instruction}]}

    if not tts_mode and not image_gen:
        tools = []
        if use_search:
            tools.append({"googleSearch": {}})
        if use_code_exec:
            tools.append({"codeExecution": {}})
        if tools:
            body["tools"] = tools

    def generate():
        resp = req_lib.post(url, headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}, json=body, stream=True)
        if not resp.ok:
            try:
                error_msg = resp.json().get("error", {}).get("message", resp.text)
            except Exception:
                error_msg = resp.text
            yield f"data: {json.dumps({'error': error_msg})}\n\n"
            return

        buffer = ""
        for raw_chunk in resp.iter_content(chunk_size=4096, decode_unicode=True):
            buffer += raw_chunk
            while True:
                buffer = buffer.lstrip(" ,\n\r")
                if buffer.startswith("["):
                    buffer = buffer[1:]
                    continue
                if not buffer or buffer[0] != "{":
                    clean = buffer.strip(" \n\r]")
                    if not clean:
                        buffer = ""
                    break
                depth, in_str, esc, found = 0, False, False, -1
                for i, ch in enumerate(buffer):
                    if esc:
                        esc = False; continue
                    if ch == "\\":
                        esc = True; continue
                    if ch == '"' and not esc:
                        in_str = not in_str; continue
                    if in_str:
                        continue
                    if ch == "{":
                        depth += 1
                    elif ch == "}":
                        depth -= 1
                        if depth == 0:
                            found = i; break
                if found == -1:
                    break
                obj_str = buffer[:found + 1]
                buffer = buffer[found + 1:]
                try:
                    obj = json.loads(obj_str)
                    yield f"data: {json.dumps(obj)}\n\n"
                except json.JSONDecodeError:
                    continue
        yield "data: [DONE]\n\n"

    return Response(stream_with_context(generate()), content_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ── PCM-to-WAV ──────────────────────────────────────────────────────────────

@app.route("/api/pcm-to-wav", methods=["POST"])
def pcm_to_wav():
    data = request.json
    pcm_input = data.get("data", "")
    sample_rate = data.get("sampleRate", 24000)
    channels = data.get("channels", 1)
    bits = data.get("bitsPerSample", 16)
    try:
        if isinstance(pcm_input, list):
            pcm = b"".join(base64.b64decode(chunk) for chunk in pcm_input)
        else:
            pcm = base64.b64decode(pcm_input)
        byte_rate = sample_rate * channels * bits // 8
        block_align = channels * bits // 8
        wav_header = struct.pack('<4sI4s4sIHHIIHH4sI',
            b'RIFF', 36 + len(pcm), b'WAVE',
            b'fmt ', 16, 1, channels, sample_rate, byte_rate, block_align, bits,
            b'data', len(pcm))
        wav_b64 = base64.b64encode(wav_header + pcm).decode()
        return jsonify({"data": wav_b64, "mimeType": "audio/wav"})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


# ── Media ───────────────────────────────────────────────────────────────────

@app.route("/api/chats/save-media", methods=["POST"])
@require_auth
def api_save_media():
    data = request.json
    media_data = data.get("data", "")
    mime_type = data.get("mimeType", "")
    if not media_data:
        return jsonify({"error": "Missing params"}), 400
    media_id = str(uuid.uuid4()).replace("-", "")[:12]
    save_media(request.user_email, media_id, media_data, mime_type)
    return jsonify({"ok": True, "mediaId": media_id})


@app.route("/api/chats/load-media", methods=["POST"])
@require_auth
def api_load_media():
    media_id = safe_id((request.json or {}).get("mediaId", ""))
    if not media_id:
        return jsonify({"error": "Missing params"}), 400
    result = load_media(request.user_email, media_id)
    if not result:
        return jsonify({"error": "Not found"}), 404
    return jsonify(result)


# ── Main ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not MONGODB_URI:
        DATA_DIR.mkdir(exist_ok=True)
        (DATA_DIR / "users").mkdir(exist_ok=True)
        print(f"\n  WARNING: No MONGODB_URI set. Using local file storage at {DATA_DIR.resolve()}")
        if not ENCRYPTION_KEY:
            print("  WARNING: No ENCRYPTION_KEY set. SA keys stored with base64 encoding only.")
    else:
        print(f"\n  Connected to MongoDB")
    if not GOOGLE_CLIENT_ID:
        print("  Google Sign-In disabled (no GOOGLE_CLIENT_ID)")
    if DEV_LOGIN:
        print("  Dev login ENABLED (set DEV_LOGIN=false for production)")
    print()
    app.run(debug=False, host="127.0.0.1", port=5000)
