import json
import os
from flask import Flask, render_template, request, Response, stream_with_context
from google.oauth2 import service_account
import google.auth.transport.requests

app = Flask(__name__)

VERTEX_AI_URL = "https://{location}-aiplatform.googleapis.com/v1beta1/projects/{project_id}/locations/{location}/publishers/google/models/{model}:streamGenerateContent"

MODEL_IDS = {
    "gemini-2.5-pro": "gemini-2.5-pro",
    "gemini-2.5-flash": "gemini-2.5-flash",
}


def get_access_token(sa_key_data):
    """Exchange service account JSON key for an access token."""
    credentials = service_account.Credentials.from_service_account_info(
        sa_key_data,
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )
    credentials.refresh(google.auth.transport.requests.Request())
    return credentials.token


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/chat", methods=["POST"])
def chat():
    data = request.json
    sa_key_raw = data.get("serviceAccountKey", "")
    project_id = data.get("projectId", "").strip()
    location = data.get("location", "us-central1").strip()
    model_key = data.get("model", "gemini-2.5-flash")
    messages = data.get("messages", [])
    temperature = data.get("temperature", 1.0)
    max_tokens = data.get("maxTokens", 8192)

    if not sa_key_raw or not project_id:
        return {"error": "Missing service account key or project ID"}, 400

    model_id = MODEL_IDS.get(model_key)
    if not model_id:
        return {"error": f"Unknown model: {model_key}"}, 400

    if not messages:
        return {"error": "No messages provided"}, 400

    # Parse service account key
    try:
        sa_key_data = json.loads(sa_key_raw) if isinstance(sa_key_raw, str) else sa_key_raw
    except json.JSONDecodeError:
        return {"error": "Invalid service account JSON key"}, 400

    # Get access token
    try:
        access_token = get_access_token(sa_key_data)
    except Exception as e:
        return {"error": f"Auth failed: {e}"}, 401

    # Build Vertex AI request
    url = VERTEX_AI_URL.format(
        location=location,
        project_id=project_id,
        model=model_id,
    )

    body = {
        "contents": messages,
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": max_tokens,
        },
    }

    def generate():
        import requests as req_lib

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
            error_text = resp.text
            try:
                error_msg = resp.json().get("error", {}).get("message", error_text)
            except Exception:
                error_msg = error_text
            yield f"data: {json.dumps({'error': error_msg})}\n\n"
            return

        # Vertex AI streams a JSON array: [{...},\n,{...},\n...]
        # We parse each complete JSON object and forward as SSE
        buffer = ""
        for chunk in resp.iter_content(chunk_size=None, decode_unicode=True):
            buffer += chunk

            # Try to extract complete JSON objects from the buffer
            # Strip array brackets and commas between objects
            while buffer:
                buffer = buffer.lstrip(" ,[\n\r")
                if not buffer or buffer[0] != "{":
                    # Check if we're at the end (just ] left)
                    stripped = buffer.strip(" \n\r]")
                    if not stripped:
                        buffer = ""
                        break
                    break

                # Find matching closing brace
                depth = 0
                i = 0
                in_string = False
                escape = False
                for i, ch in enumerate(buffer):
                    if escape:
                        escape = False
                        continue
                    if ch == "\\":
                        escape = True
                        continue
                    if ch == '"':
                        in_string = not in_string
                        continue
                    if in_string:
                        continue
                    if ch == "{":
                        depth += 1
                    elif ch == "}":
                        depth -= 1
                        if depth == 0:
                            break

                if depth != 0:
                    # Incomplete JSON object, wait for more data
                    break

                obj_str = buffer[: i + 1]
                buffer = buffer[i + 1 :]

                try:
                    obj = json.loads(obj_str)
                    yield f"data: {json.dumps(obj)}\n\n"
                except json.JSONDecodeError:
                    pass

        yield "data: [DONE]\n\n"

    return Response(
        stream_with_context(generate()),
        content_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


if __name__ == "__main__":
    app.run(debug=True, host="127.0.0.1", port=5000)
