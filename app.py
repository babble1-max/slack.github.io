"""
Slack Daily Scheduler — 設定Web UI

PCまたはスマホのブラウザから http://localhost:5000 を開いて設定します。
実際の定時送信は GitHub Actions が行うため、PCがオフでも動作します。

Required Slack Bot OAuth Scopes:
  - channels:read       : list public channels
  - groups:read         : list private channels
  - files:write         : upload image files
  - chat:write          : post messages
  - chat:write.public   : post to channels the bot hasn't joined
"""

import json
import logging
import os
from pathlib import Path

import requests
from flask import Flask, jsonify, render_template, request
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from werkzeug.utils import secure_filename

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# コミットされる設定（チャンネル・メッセージ・画像など）
PUBLIC_CONFIG_FILE = Path("slack_config.json")
# コミットしない設定（トークン・ローカルログ）
PRIVATE_CONFIG_FILE = Path("config.json")

IMAGES_DIR = Path("images")
IMAGES_DIR.mkdir(exist_ok=True)

ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}

DEFAULT_PUBLIC = {
    "channel_id":      "",
    "channel_name":    "",
    "message":         "",
    "image_filename":  "",
    "send_time":       "09:00",
}

DEFAULT_PRIVATE = {
    "token": "",
    "log":   [],
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def load_public() -> dict:
    if not PUBLIC_CONFIG_FILE.exists():
        return DEFAULT_PUBLIC.copy()
    try:
        return {**DEFAULT_PUBLIC, **json.loads(PUBLIC_CONFIG_FILE.read_text(encoding="utf-8"))}
    except (json.JSONDecodeError, OSError):
        return DEFAULT_PUBLIC.copy()


def save_public(data: dict) -> None:
    keys = list(DEFAULT_PUBLIC.keys())
    out  = {k: data[k] for k in keys if k in data}
    PUBLIC_CONFIG_FILE.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")


def load_private() -> dict:
    if not PRIVATE_CONFIG_FILE.exists():
        return DEFAULT_PRIVATE.copy()
    try:
        return {**DEFAULT_PRIVATE, **json.loads(PRIVATE_CONFIG_FILE.read_text(encoding="utf-8"))}
    except (json.JSONDecodeError, OSError):
        return DEFAULT_PRIVATE.copy()


def save_private(data: dict) -> None:
    PRIVATE_CONFIG_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Slack send logic
# ---------------------------------------------------------------------------

def send_slack_message() -> dict:
    pub     = load_public()
    priv    = load_private()
    token   = priv.get("token", "")
    channel = pub.get("channel_id", "")
    message = pub.get("message", "")
    image_path = IMAGES_DIR / pub["image_filename"] if pub.get("image_filename") else None

    result = {"status": "", "detail": ""}

    if not token or not channel:
        result["status"] = "error"
        result["detail"] = "トークンまたはチャンネルが設定されていません"
        return result

    client = WebClient(token=token)

    try:
        if image_path and image_path.exists():
            file_size = image_path.stat().st_size
            resp = client.files_getUploadURLExternal(
                filename=image_path.name,
                length=file_size,
            )
            upload_url = resp["upload_url"]
            file_id    = resp["file_id"]

            with open(image_path, "rb") as f:
                put_resp = requests.put(
                    upload_url,
                    data=f,
                    headers={"Content-Type": "application/octet-stream"},
                    timeout=60,
                )
            put_resp.raise_for_status()

            client.files_completeUploadExternal(
                files=[{"id": file_id, "title": image_path.name}],
                channel_id=channel,
                initial_comment=message,
            )
        else:
            client.chat_postMessage(channel=channel, text=message)

        result["status"] = "success"
        result["detail"] = f"{pub.get('channel_name', channel)} に送信しました"

    except SlackApiError as e:
        result["status"] = "error"
        result["detail"] = f"Slack APIエラー: {e.response['error']}"
    except requests.HTTPError as e:
        result["status"] = "error"
        result["detail"] = f"ファイルアップロード失敗: {e}"
    except Exception as e:
        result["status"] = "error"
        result["detail"] = str(e)

    return result


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

app = Flask(__name__)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/config", methods=["GET"])
def api_config_get():
    pub  = load_public()
    priv = load_private()
    return jsonify({
        **pub,
        "token": "***" if priv.get("token") else "",
    })


@app.route("/api/config", methods=["POST"])
def api_config_post():
    data = request.get_json(force=True)
    pub  = load_public()
    priv = load_private()

    if data.get("token") and data["token"] != "***":
        priv["token"] = data["token"]
        save_private(priv)

    for key in DEFAULT_PUBLIC:
        if key in data:
            pub[key] = data[key]
    save_public(pub)

    return jsonify({"ok": True})


@app.route("/api/channels")
def api_channels():
    priv  = load_private()
    token = request.args.get("token") or priv.get("token")
    if not token or token == "***":
        return jsonify({"error": "トークンが設定されていません"}), 400

    try:
        client   = WebClient(token=token)
        channels = []
        cursor   = None
        while True:
            resp = client.conversations_list(
                types="public_channel,private_channel",
                limit=200,
                cursor=cursor,
            )
            channels.extend(resp["channels"])
            cursor = resp.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
        result = sorted(
            [{"id": c["id"], "name": c["name"]} for c in channels],
            key=lambda c: c["name"],
        )
        return jsonify({"channels": result})
    except SlackApiError as e:
        return jsonify({"error": e.response["error"]}), 400


@app.route("/api/upload-image", methods=["POST"])
def api_upload_image():
    if "file" not in request.files:
        return jsonify({"error": "ファイルが見つかりません"}), 400
    file = request.files["file"]
    if not file.filename:
        return jsonify({"error": "ファイル名が空です"}), 400

    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        return jsonify({"error": f"許可されていない拡張子です: {ext}"}), 400

    filename  = secure_filename(file.filename)
    save_path = IMAGES_DIR / filename
    file.save(save_path)

    pub = load_public()
    pub["image_filename"] = filename
    save_public(pub)

    return jsonify({"ok": True, "filename": filename})


@app.route("/api/send-now", methods=["POST"])
def api_send_now():
    result      = send_slack_message()
    status_code = 200 if result["status"] == "success" else 500
    return jsonify(result), status_code


@app.route("/api/log")
def api_log():
    priv = load_private()
    return jsonify({"log": priv.get("log", [])[:10]})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", 5000))
    # スマホからもアクセスできるよう 0.0.0.0 でリッスン
    app.run(debug=False, host=host, port=port)
