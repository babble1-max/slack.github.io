"""
Slack Daily Sender — GitHub Actions から呼び出されるスタンドアロンスクリプト

環境変数:
  SLACK_TOKEN : Slack Bot Token (GitHub Secret に設定)

設定ファイル:
  slack_config.json : チャンネル・メッセージ・画像ファイル名
"""

import json
import os
import sys
from pathlib import Path

import requests
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

CONFIG_FILE = Path("slack_config.json")
IMAGES_DIR  = Path("images")


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        print("❌ slack_config.json が見つかりません", file=sys.stderr)
        sys.exit(1)
    return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))


def send(token: str, config: dict) -> None:
    channel        = config.get("channel_id", "")
    message        = config.get("message", "")
    image_filename = config.get("image_filename", "")
    image_path     = IMAGES_DIR / image_filename if image_filename else None

    if not channel:
        print("❌ channel_id が設定されていません", file=sys.stderr)
        sys.exit(1)

    client = WebClient(token=token)

    if image_path and image_path.exists():
        file_size = image_path.stat().st_size

        # Step 1: 署名付きアップロードURL取得
        resp = client.files_getUploadURLExternal(
            filename=image_path.name,
            length=file_size,
        )
        upload_url = resp["upload_url"]
        file_id    = resp["file_id"]

        # Step 2: ファイルをPUT
        with open(image_path, "rb") as f:
            put_resp = requests.put(
                upload_url,
                data=f,
                headers={"Content-Type": "application/octet-stream"},
                timeout=60,
            )
        put_resp.raise_for_status()

        # Step 3: アップロード完了・チャンネルへ投稿
        client.files_completeUploadExternal(
            files=[{"id": file_id, "title": image_path.name}],
            channel_id=channel,
            initial_comment=message,
        )
        print(f"✅ 画像付きメッセージを送信しました → #{config.get('channel_name', channel)}")
    else:
        if image_filename and not (image_path and image_path.exists()):
            print(f"⚠️  画像ファイルが見つかりません: {image_path}", file=sys.stderr)
        client.chat_postMessage(channel=channel, text=message)
        print(f"✅ メッセージを送信しました → #{config.get('channel_name', channel)}")


def main() -> None:
    token = os.environ.get("SLACK_TOKEN", "")
    if not token:
        print("❌ 環境変数 SLACK_TOKEN が設定されていません", file=sys.stderr)
        sys.exit(1)

    config = load_config()

    try:
        send(token, config)
    except SlackApiError as e:
        print(f"❌ Slack APIエラー: {e.response['error']}", file=sys.stderr)
        sys.exit(1)
    except requests.HTTPError as e:
        print(f"❌ ファイルアップロード失敗: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"❌ エラー: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
