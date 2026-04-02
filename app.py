import json
import os
import hashlib
import time
import uuid
import struct
import base64
from pathlib import Path

from flask import Flask, render_template, request, Response, stream_with_context, jsonify
from google.oauth2 import service_account
import google.auth.transport.requests
import requests as req_lib

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "dev-secret-change-in-prod")
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB max request

DATA_DIR = Path(os.environ.get("DATA_DIR", "local_data"))
DATA_DIR.mkdir(exist_ok=True)
(DATA_DIR / "users").mkdir(exist_ok=True)

# ── Models ──
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


def user_dir(email):
    h = hashlib.sha256(email.encode()).hexdigest()[:16]
    d = DATA_DIR / "users" / h
    d.mkdir(exist_ok=True)
    return d


def chats_dir(email):
    d = user_dir(email) / "chats"
    d.mkdir(exist_ok=True)
    return d


def save_user_sa_key(email, key_data):
    d = user_dir(email)
    with open(d / "sa_key.json", "w") as f:
        json.dump(key_data, f)
    with open(d / "config.json", "w") as f:
        json.dump({"project_id": key_data.get("project_id", ""), "client_email": key_data.get("client_email", ""), "updated_at": time.time()}, f)


def load_user_sa_key(email):
    d = user_dir(email)
    key_path, config_path = d / "sa_key.json", d / "config.json"
    if key_path.exists():
        with open(key_path) as f:
            key = json.load(f)
        config = {}
        if config_path.exists():
            with open(config_path) as f:
                config = json.load(f)
        return key, config
    return None, None


def get_access_token(sa_key_data):
    creds = service_account.Credentials.from_service_account_info(sa_key_data, scopes=["https://www.googleapis.com/auth/cloud-platform"])
    creds.refresh(google.auth.transport.requests.Request())
    return creds.token


def generate_topic(text):
    """Generate a short topic from the first user message."""
    words = text.strip().split()
    if len(words) <= 6:
        return text.strip()
    return " ".join(words[:6]) + "..."


# ── Routes ──

@app.route("/")
def index():
    return render_template("index.html", models=MODELS)


@app.route("/api/models")
def get_models():
    return jsonify(MODELS)


@app.route("/api/save-key", methods=["POST"])
def save_key():
    data = request.json
    email, sa_key_raw = data.get("email", "").strip(), data.get("serviceAccountKey", "")
    if not email or not sa_key_raw:
        return jsonify({"error": "Missing email or key"}), 400
    try:
        sa_key = json.loads(sa_key_raw) if isinstance(sa_key_raw, str) else sa_key_raw
    except json.JSONDecodeError:
        return jsonify({"error": "Invalid JSON key"}), 400
    try:
        get_access_token(sa_key)
    except Exception as e:
        return jsonify({"error": f"Key validation failed: {e}"}), 400
    save_user_sa_key(email, sa_key)
    return jsonify({"ok": True, "project_id": sa_key.get("project_id", ""), "client_email": sa_key.get("client_email", "")})


@app.route("/api/check-key", methods=["POST"])
def check_key():
    data = request.json
    email = data.get("email", "").strip()
    if not email:
        return jsonify({"has_key": False})
    _, config = load_user_sa_key(email)
    if config:
        return jsonify({"has_key": True, "project_id": config.get("project_id", ""), "client_email": config.get("client_email", "")})
    return jsonify({"has_key": False})


# ── Chat History ──

@app.route("/api/chats", methods=["POST"])
def list_chats():
    email = request.json.get("email", "").strip()
    if not email:
        return jsonify([])
    d = chats_dir(email)
    chats = []
    for f in sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        with open(f) as fp:
            meta = json.load(fp)
        chats.append({"id": f.stem, "topic": meta.get("topic", "Untitled"), "model": meta.get("model", ""), "updated": meta.get("updated", 0), "messageCount": len(meta.get("messages", []))})
    return jsonify(chats)


@app.route("/api/chats/save", methods=["POST"])
def save_chat():
    data = request.json
    email = data.get("email", "").strip()
    chat_id = data.get("id") or str(uuid.uuid4())[:8]
    if not email:
        return jsonify({"error": "Not authenticated"}), 401
    d = chats_dir(email)
    chat_data = {
        "id": chat_id,
        "topic": data.get("topic", "Untitled"),
        "model": data.get("model", ""),
        "messages": data.get("messages", []),
        "settings": data.get("settings", {}),
        "systemPrompt": data.get("systemPrompt", ""),
        "updated": time.time(),
        "totIn": data.get("totIn", 0),
        "totOut": data.get("totOut", 0),
    }
    with open(d / f"{chat_id}.json", "w") as f:
        json.dump(chat_data, f)
    return jsonify({"ok": True, "id": chat_id})


@app.route("/api/chats/load", methods=["POST"])
def load_chat():
    data = request.json
    email, chat_id = data.get("email", "").strip(), data.get("id", "")
    if not email or not chat_id:
        return jsonify({"error": "Missing params"}), 400
    path = chats_dir(email) / f"{chat_id}.json"
    if not path.exists():
        return jsonify({"error": "Chat not found"}), 404
    with open(path) as f:
        return jsonify(json.load(f))


@app.route("/api/chats/delete", methods=["POST"])
def delete_chat():
    data = request.json
    email, chat_id = data.get("email", "").strip(), data.get("id", "")
    if not email or not chat_id:
        return jsonify({"error": "Missing params"}), 400
    path = chats_dir(email) / f"{chat_id}.json"
    if path.exists():
        path.unlink()
    return jsonify({"ok": True})


@app.route("/api/chats/rename", methods=["POST"])
def rename_chat():
    data = request.json
    email, chat_id, topic = data.get("email", "").strip(), data.get("id", ""), data.get("topic", "")
    if not email or not chat_id:
        return jsonify({"error": "Missing params"}), 400
    path = chats_dir(email) / f"{chat_id}.json"
    if not path.exists():
        return jsonify({"error": "Not found"}), 404
    with open(path) as f:
        chat = json.load(f)
    chat["topic"] = topic
    with open(path, "w") as f:
        json.dump(chat, f)
    return jsonify({"ok": True})


# ── Chat API ──

@app.route("/api/chat", methods=["POST"])
def chat():
    data = request.json
    email = data.get("email", "").strip()
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

    # Auto-detect mode from model ID if frontend didn't set flags
    model_group = MODELS.get(model_id, {}).get("group", "chat")
    if model_group == "tts":
        tts_mode = True
    elif model_group == "image":
        image_gen = True

    if not email:
        return jsonify({"error": "Not authenticated"}), 401
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

    # Build generationConfig from all provided params
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

    # TTS mode overrides — force TTS model if needed
    if tts_mode:
        if "tts" not in model_id:
            model_id = "gemini-2.5-flash-preview-tts"
            model_info = MODELS[model_id]
            url = REGIONAL_URL.format(location=location, project_id=project_id, model=model_id)
        final_config["responseModalities"] = ["AUDIO"]
        final_config["speechConfig"] = {
            "voiceConfig": {
                "prebuiltVoiceConfig": {"voiceName": tts_voice}
            }
        }
        # TTS doesn't support system instructions
        system_instruction = ""

    # Image generation mode — force image model if needed
    if image_gen:
        if "image" not in model_id:
            model_id = "gemini-2.5-flash-image"
            model_info = MODELS[model_id]
            url = REGIONAL_URL.format(location=location, project_id=project_id, model=model_id)
        final_config["responseModalities"] = ["TEXT", "IMAGE"]

    body = {"contents": messages, "generationConfig": final_config}

    if system_instruction:
        body["systemInstruction"] = {"parts": [{"text": system_instruction}]}

    # Build tools array — TTS and image gen don't support tools
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
                depth = 0
                in_str = False
                esc = False
                found = -1
                for i, ch in enumerate(buffer):
                    if esc: esc = False; continue
                    if ch == "\\": esc = True; continue
                    if ch == '"' and not esc: in_str = not in_str; continue
                    if in_str: continue
                    if ch == "{": depth += 1
                    elif ch == "}":
                        depth -= 1
                        if depth == 0: found = i; break
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


@app.route("/api/pcm-to-wav", methods=["POST"])
def pcm_to_wav():
    """Convert raw PCM base64 (single or array of chunks) to WAV base64."""
    data = request.json
    pcm_input = data.get("data", "")
    sample_rate = data.get("sampleRate", 24000)
    channels = data.get("channels", 1)
    bits = data.get("bitsPerSample", 16)
    try:
        # Handle array of base64 chunks or single string
        if isinstance(pcm_input, list):
            pcm = b"".join(base64.b64decode(chunk) for chunk in pcm_input)
        else:
            pcm = base64.b64decode(pcm_input)
        # Build WAV header
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


@app.route("/api/chats/save-media", methods=["POST"])
def save_media():
    """Save large media separately and return a reference ID."""
    data = request.json
    email = data.get("email", "").strip()
    media_data = data.get("data", "")
    mime_type = data.get("mimeType", "")
    if not email or not media_data:
        return jsonify({"error": "Missing params"}), 400
    media_id = str(uuid.uuid4())[:12]
    media_dir = user_dir(email) / "media"
    media_dir.mkdir(exist_ok=True)
    with open(media_dir / f"{media_id}.json", "w") as f:
        json.dump({"data": media_data, "mimeType": mime_type}, f)
    return jsonify({"ok": True, "mediaId": media_id})


@app.route("/api/chats/load-media", methods=["POST"])
def load_media():
    """Load saved media by ID."""
    data = request.json
    email = data.get("email", "").strip()
    media_id = data.get("mediaId", "")
    if not email or not media_id:
        return jsonify({"error": "Missing params"}), 400
    path = user_dir(email) / "media" / f"{media_id}.json"
    if not path.exists():
        return jsonify({"error": "Not found"}), 404
    with open(path) as f:
        return jsonify(json.load(f))


if __name__ == "__main__":
    print(f"\n  Data stored in: {DATA_DIR.resolve()}")
    app.run(debug=False, host="127.0.0.1", port=5000)
