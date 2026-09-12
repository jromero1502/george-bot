#!/usr/bin/env python
"""POSTs a synthetic Telegram update to the locally running Function App, so
you can exercise the whole pipeline (webhook -> queue -> agent -> reply)
without a real Telegram bot or bot token.

Requires `func start` to already be running (see scripts/local_up.ps1) and
local.settings.json's DRY_RUN=true (so telegram.send_message just logs
instead of calling a real API).

Examples:
    python scripts/simulate_update.py --text "cuanto me debe Maria"
    python scripts/simulate_update.py --voice ./samples/nota.ogg
    python scripts/simulate_update.py --text "hola" --chat 999999999   # unauthorized chat -> 200, dropped
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import time
from typing import Any, Optional

import httpx

_counter = itertools.count(start=int(time.time()))


def build_update(chat_id: str, text: Optional[str], voice_path: Optional[str]) -> dict[str, Any]:
    message: dict[str, Any] = {
        "message_id": next(_counter),
        "date": int(time.time()),
        "chat": {"id": int(chat_id), "type": "private"},
        "from": {"id": int(chat_id), "first_name": "Tester", "is_bot": False},
    }

    if voice_path:
        message["voice"] = {
            "file_id": "LOCAL-FAKE-FILE-ID",
            "file_unique_id": "LOCAL-FAKE-FILE-ID",
            "duration": 3,
            "mime_type": "audio/ogg",
            "file_size": os.path.getsize(voice_path),
        }
        # See function_app.telegram_webhook / process_update: a real Telegram
        # update never has this field, but our webhook forwards it verbatim
        # to the queue message so process_update can skip getFile/download.
        message["localAudioPath"] = os.path.abspath(voice_path)
    else:
        message["text"] = text

    return {"update_id": next(_counter), "message": message}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--text", help="Simulate a text message")
    parser.add_argument("--voice", help="Path to a local .ogg/.mp3 file to simulate a voice note")
    parser.add_argument("--chat", default=os.environ.get("PLATFORM_ADMIN_CHAT_ID", "111111111"))
    parser.add_argument("--base-url", default="http://localhost:7071")
    parser.add_argument("--path", default=os.environ.get("TELEGRAM_WEBHOOK_PATH", "local-dev-path"))
    parser.add_argument("--secret", default=os.environ.get("TELEGRAM_WEBHOOK_SECRET", "local-dev-secret"))
    parser.add_argument("--function-key", default=None, help="Only needed against a deployed app (authLevel=function)")
    args = parser.parse_args()

    if not args.text and not args.voice:
        parser.error("pass --text or --voice")
    if args.voice and not os.path.isfile(args.voice):
        parser.error(f"--voice file not found: {args.voice}")

    update = build_update(args.chat, args.text, args.voice)

    url = f"{args.base_url}/api/telegram/{args.path}"
    if args.function_key:
        url += f"?code={args.function_key}"
    headers = {"X-Telegram-Bot-Api-Secret-Token": args.secret}

    print(f"POST {url}")
    print(json.dumps(update, indent=2, ensure_ascii=False))

    response = httpx.post(url, json=update, headers=headers, timeout=30.0)
    print(f"\n<- {response.status_code} {response.text or '(empty body)'}")
    print("\nCheck the `func start` terminal for George's reply (DRY_RUN logs sendMessage instead of calling Telegram).")


if __name__ == "__main__":
    main()
