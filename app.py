"""
Slack Daily Scheduled Sender

Required Slack Bot OAuth Scopes:
  - channels:read       : list public channels
  - groups:read         : list private channels
  - files:write         : upload image files
  - chat:write          : post messages
  - chat:write.public   : post to channels the bot hasn't joined
"""

import atexit
import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from flask import Flask, jsonify, render_template, request
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from werkzeug.utils import secure_filename
import tzlocal

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CONFIG_FILE = Path("config.json")
UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(exist_ok=True)

DEFAULT_CONFIG = {
    "token": "",
    "channel_id": "",
    "channel_name": "",
    "message": "",
    "image_filename": "",
    "send_time": "09:00",
    "enabled": False,
    "last_sent": None,
    "log": [],
}

ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config helpers (thread-safe)
# ---------------------------------------------------------------------------

_config_lock = threading.Lock()


def load_config() -> dict:
    with _config_lock:
        if not CONFIG_FILE.exists():
            return DEFAULT_CONFIG.copy()
        try:
            with open(CONFIG_FILE) as f:
                return {**DEFAULT_CONFIG, **json.load(f)}
        except (json.JSONDecodeError, OSError):
            logger.warning("config.json が読めませんでした。デフォルト設定を使用します。")
            return DEFAULT_CONFIG.copy()


def save_config(data: dict) -> None:
    with _config_lock:
        with open(CONFIG_FILE, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Slack send logic
# ---------------------------------------------------------------------------

def send_slack_message() -> dict:
    config = load_config()
    token = config.get("token", "")
    channel = config.get("channel_id", "")
    message = config.get("message", "")
    image_filename = config.get("image_filename", "")
    image_path = UPLOAD_DIR / image_filename if image_filename else None

    log_entry = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "status": "",
        "detail": "",
    }

    if not token or not channel:
        log_entry["status"] = "error"
        log_entry["detail"] = "トークンまたはチャンネルが設定されていません"
        _append_log(log_entry)
        return log_entry

    client = WebClient(token=token)

    try:
        if image_path and image_path.exists():
            # Step 1: 署名付きアップロードURL取得
            file_size = image_path.stat().st_size
            resp = client.files_getUploadURLExternal(
                filename=image_path.name,
                length=file_size,
            )
            upload_url = resp["upload_url"]
            file_id = resp["file_id"]

            # Step 2: ファイルをPUT
            with open(image_path, "rb") as f:
                put_resp = requests.put(
                    upload_url,
                    data=f,
                    headers={"Content-Type": "application/octet-stream"},
                    timeout=60,
                )
            put_resp.raise_for_status()

            # Step 3: アップロード完了・チャンネルに送信
            client.files_completeUploadExternal(
                files=[{"id": file_id, "title": image_path.name}],
                channel_id=channel,
                initial_comment=message,
            )
        else:
            client.chat_postMessage(channel=channel, text=message)

        log_entry["status"] = "success"
        log_entry["detail"] = f"{config.get('channel_name', channel)} に送信しました"

    except SlackApiError as e:
        log_entry["status"] = "error"
        log_entry["detail"] = f"Slack APIエラー: {e.response['error']}"
    except requests.HTTPError as e:
        log_entry["status"] = "error"
        log_entry["detail"] = f"ファイルアップロード失敗: {e}"
    except Exception as e:
        log_entry["status"] = "error"
        log_entry["detail"] = str(e)

    _append_log(log_entry)
    return log_entry


def _append_log(entry: dict) -> None:
    config = load_config()
    config["last_sent"] = entry["time"]
    logs = config.get("log", [])
    logs.insert(0, entry)
    config["log"] = logs[:50]
    save_config(config)


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

scheduler = BackgroundScheduler(timezone=tzlocal.get_localzone(), daemon=True)
scheduler.start()
atexit.register(lambda: scheduler.shutdown(wait=False))


def reschedule(config: dict) -> None:
    if not config.get("enabled") or not config.get("send_time"):
        if scheduler.get_job("daily_slack"):
            scheduler.remove_job("daily_slack")
        return

    try:
        hour, minute = config["send_time"].split(":")
        scheduler.add_job(
            func=send_slack_message,
            trigger=CronTrigger(hour=int(hour), minute=int(minute)),
            id="daily_slack",
            replace_existing=True,
            misfire_grace_time=3600,
        )
        logger.info(f"スケジュール設定: 毎日 {config['send_time']}")
    except (ValueError, KeyError) as e:
        logger.error(f"スケジュール設定失敗: {e}")


# 起動時に保存済み設定でスケジュール復元
reschedule(load_config())

# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

app = Flask(__name__)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/config", methods=["GET"])
def api_config_get():
    config = load_config()
    # トークンをマスク
    result = {**config}
    if result.get("token"):
        result["token"] = "***"
    result.pop("log", None)
    return jsonify(result)


@app.route("/api/config", methods=["POST"])
def api_config_post():
    data = request.get_json(force=True)
    config = load_config()

    # トークンはマスク値が来た場合は既存値を保持
    if data.get("token") and data["token"] != "***":
        config["token"] = data["token"]

    for key in ("channel_id", "channel_name", "message", "send_time", "enabled"):
        if key in data:
            config[key] = data[key]

    save_config(config)
    reschedule(config)
    return jsonify({"ok": True})


@app.route("/api/channels")
def api_channels():
    config = load_config()
    token = request.args.get("token") or config.get("token")
    if not token or token == "***":
        return jsonify({"error": "トークンが設定されていません"}), 400

    try:
        client = WebClient(token=token)
        channels = []
        cursor = None
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
        result = [{"id": c["id"], "name": c["name"]} for c in channels]
        result.sort(key=lambda c: c["name"])
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

    filename = secure_filename(file.filename)
    save_path = UPLOAD_DIR / filename
    file.save(save_path)

    config = load_config()
    config["image_filename"] = filename
    save_config(config)

    return jsonify({"ok": True, "filename": filename})


@app.route("/api/send-now", methods=["POST"])
def api_send_now():
    result = send_slack_message()
    status_code = 200 if result["status"] == "success" else 500
    return jsonify(result), status_code


@app.route("/api/status")
def api_status():
    job = scheduler.get_job("daily_slack")
    next_run = None
    if job and job.next_run_time:
        next_run = job.next_run_time.strftime("%Y-%m-%d %H:%M:%S")

    config = load_config()
    return jsonify({
        "enabled": config.get("enabled", False),
        "next_run": next_run,
        "last_sent": config.get("last_sent"),
        "log": config.get("log", [])[:10],
    })


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(debug=True, use_reloader=False, port=5000)
