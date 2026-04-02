import json
import os
import hashlib
import time
import uuid
from pathlib import Path

from flask import Flask, render_template, request, Response, stream_with_context, jsonify
from google.oauth2 import service_account
import google.auth.transport.requests
import requests as req_lib

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "dev-secret-change-in-prod")

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
    system_instruction = data.get("systemInstruction", "")

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

    body = {"contents": messages, "generationConfig": final_config}

    if system_instruction:
        body["systemInstruction"] = {"parts": [{"text": system_instruction}]}

    if use_search:
        body["tools"] = [{"googleSearch": {}}]

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


if __name__ == "__main__":
    print(f"\n  Data stored in: {DATA_DIR.resolve()}")
    app.run(debug=False, host="127.0.0.1", port=5000)
