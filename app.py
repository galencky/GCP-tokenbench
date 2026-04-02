import json
import os
import hashlib
import time
from pathlib import Path

from flask import Flask, render_template, request, Response, stream_with_context, jsonify, session
from google.oauth2 import service_account
import google.auth.transport.requests
import requests as req_lib

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "dev-secret-change-in-prod")

# Local storage directory
DATA_DIR = Path(os.environ.get("DATA_DIR", "local_data"))
DATA_DIR.mkdir(exist_ok=True)
(DATA_DIR / "users").mkdir(exist_ok=True)
(DATA_DIR / "keys").mkdir(exist_ok=True)

# ── Models ──
MODELS = {
    "gemini-3.1-pro-preview": {
        "name": "Gemini 3.1 Pro (Preview)",
        "input_price": 2.00,
        "output_price": 12.00,
        "context": 1048576,
        "category": "Best reasoning",
        "global": True,
    },
    "gemini-3-flash-preview": {
        "name": "Gemini 3 Flash (Preview)",
        "input_price": 0.50,
        "output_price": 3.00,
        "context": 1048576,
        "category": "Fast + smart",
        "global": True,
    },
    "gemini-3.1-flash-lite-preview": {
        "name": "Gemini 3.1 Flash Lite (Preview)",
        "input_price": 0.25,
        "output_price": 1.50,
        "context": 1048576,
        "category": "Cheapest 3.x",
        "global": True,
    },
    "gemini-2.5-pro": {
        "name": "Gemini 2.5 Pro",
        "input_price": 1.25,
        "output_price": 10.00,
        "context": 1048576,
        "category": "Stable reasoning",
        "global": False,
    },
    "gemini-2.5-flash": {
        "name": "Gemini 2.5 Flash",
        "input_price": 0.30,
        "output_price": 2.50,
        "context": 1048576,
        "category": "Best value",
        "global": False,
    },
    "gemini-2.5-flash-lite": {
        "name": "Gemini 2.5 Flash Lite",
        "input_price": 0.10,
        "output_price": 0.40,
        "context": 1048576,
        "category": "Cheapest",
        "global": False,
    },
}

REGIONAL_URL = "https://{location}-aiplatform.googleapis.com/v1beta1/projects/{project_id}/locations/{location}/publishers/google/models/{model}:streamGenerateContent"
GLOBAL_URL = "https://aiplatform.googleapis.com/v1beta1/projects/{project_id}/locations/global/publishers/google/models/{model}:streamGenerateContent"


def user_dir(email):
    """Get user-specific directory using hashed email."""
    h = hashlib.sha256(email.encode()).hexdigest()[:16]
    d = DATA_DIR / "users" / h
    d.mkdir(exist_ok=True)
    return d


def save_user_sa_key(email, key_data):
    """Save service account key for a user."""
    d = user_dir(email)
    with open(d / "sa_key.json", "w") as f:
        json.dump(key_data, f)
    # Save project ID separately for quick access
    with open(d / "config.json", "w") as f:
        json.dump({
            "project_id": key_data.get("project_id", ""),
            "client_email": key_data.get("client_email", ""),
            "updated_at": time.time(),
        }, f)


def load_user_sa_key(email):
    """Load service account key for a user."""
    d = user_dir(email)
    key_path = d / "sa_key.json"
    config_path = d / "config.json"
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
    """Exchange service account JSON key for an access token."""
    credentials = service_account.Credentials.from_service_account_info(
        sa_key_data,
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )
    credentials.refresh(google.auth.transport.requests.Request())
    return credentials.token


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
    email = data.get("email", "").strip()
    sa_key_raw = data.get("serviceAccountKey", "")

    if not email or not sa_key_raw:
        return jsonify({"error": "Missing email or key"}), 400

    try:
        sa_key = json.loads(sa_key_raw) if isinstance(sa_key_raw, str) else sa_key_raw
    except json.JSONDecodeError:
        return jsonify({"error": "Invalid JSON key"}), 400

    # Verify the key works
    try:
        get_access_token(sa_key)
    except Exception as e:
        return jsonify({"error": f"Key validation failed: {e}"}), 400

    save_user_sa_key(email, sa_key)
    return jsonify({
        "ok": True,
        "project_id": sa_key.get("project_id", ""),
        "client_email": sa_key.get("client_email", ""),
    })


@app.route("/api/check-key", methods=["POST"])
def check_key():
    data = request.json
    email = data.get("email", "").strip()
    if not email:
        return jsonify({"has_key": False})

    _, config = load_user_sa_key(email)
    if config:
        return jsonify({
            "has_key": True,
            "project_id": config.get("project_id", ""),
            "client_email": config.get("client_email", ""),
        })
    return jsonify({"has_key": False})


@app.route("/api/chat", methods=["POST"])
def chat():
    data = request.json
    email = data.get("email", "").strip()
    model_id = data.get("model", "gemini-2.5-flash")
    location = data.get("location", "us-central1")
    messages = data.get("messages", [])
    temperature = data.get("temperature", 1.0)
    max_tokens = data.get("maxTokens", 8192)
    use_search = data.get("googleSearch", False)

    if not email:
        return jsonify({"error": "Not authenticated"}), 401

    if model_id not in MODELS:
        return jsonify({"error": f"Unknown model: {model_id}"}), 400

    if not messages:
        return jsonify({"error": "No messages"}), 400

    sa_key, _ = load_user_sa_key(email)
    if not sa_key:
        return jsonify({"error": "No service account key found. Please upload one."}), 400

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

    body = {
        "contents": messages,
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": max_tokens,
        },
    }

    if use_search:
        body["tools"] = [{"googleSearch": {}}]

    def generate():
        resp = req_lib.post(
            url,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            json=body,
            stream=True,
        )

        if not resp.ok:
            try:
                error_msg = resp.json().get("error", {}).get("message", resp.text)
            except Exception:
                error_msg = resp.text
            yield f"data: {json.dumps({'error': error_msg})}\n\n"
            return

        # Vertex AI streams a JSON array: [{...},\n{...},\n...]
        # Use incremental JSON parsing with brace counting
        buffer = ""
        for raw_chunk in resp.iter_content(chunk_size=4096, decode_unicode=True):
            buffer += raw_chunk

            while True:
                # Strip leading array/separator chars
                buffer = buffer.lstrip(" ,\n\r")
                if buffer.startswith("["):
                    buffer = buffer[1:]
                    continue
                if not buffer or buffer[0] != "{":
                    # End of array or non-object data
                    clean = buffer.strip(" \n\r]")
                    if not clean:
                        buffer = ""
                    break

                # Find complete JSON object by matching braces
                depth = 0
                in_str = False
                esc = False
                found = -1
                for i, ch in enumerate(buffer):
                    if esc:
                        esc = False
                        continue
                    if ch == "\\":
                        esc = True
                        continue
                    if ch == '"' and not esc:
                        in_str = not in_str
                        continue
                    if in_str:
                        continue
                    if ch == "{":
                        depth += 1
                    elif ch == "}":
                        depth -= 1
                        if depth == 0:
                            found = i
                            break

                if found == -1:
                    break  # incomplete object, need more data

                obj_str = buffer[:found + 1]
                buffer = buffer[found + 1:]

                try:
                    obj = json.loads(obj_str)
                    yield f"data: {json.dumps(obj)}\n\n"
                except json.JSONDecodeError:
                    continue

        yield "data: [DONE]\n\n"

    return Response(
        stream_with_context(generate()),
        content_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    print(f"\n  Data stored in: {DATA_DIR.resolve()}")
    app.run(debug=False, host="127.0.0.1", port=5000)
