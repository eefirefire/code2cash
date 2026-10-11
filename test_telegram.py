"""
One-command Telegram connectivity check.

    TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... python test_telegram.py

PowerShell:
    $env:TELEGRAM_BOT_TOKEN="..."; $env:TELEGRAM_CHAT_ID="..."; python test_telegram.py
"""
import sys
from datetime import datetime

import requests

from config import settings as cfg
from core import notifier


def main() -> int:
    if not cfg.TELEGRAM_BOT_TOKEN or not cfg.TELEGRAM_CHAT_ID:
        print("❌ Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID environment variables first.")
        return 1

    me = requests.get(f"https://api.telegram.org/bot{cfg.TELEGRAM_BOT_TOKEN}/getMe", timeout=20)
    if not me.ok:
        print(f"❌ Bot token rejected ({me.status_code}): {me.text[:200]}")
        return 1
    bot = me.json()["result"]["username"]
    print(f"✅ Token OK — bot @{bot}")

    now = datetime.now(cfg.BKK).strftime("%Y-%m-%d %H:%M")
    text = (f"✅ <b>[Code2Cash v3] Connectivity test</b>\n"
            f"Bot @{notifier.esc(bot)} can post to this chat.\n"
            f"Sent {now} BKK")
    if notifier.send(text):
        print(f"✅ Test message delivered to chat {cfg.TELEGRAM_CHAT_ID}")
        return 0
    print("❌ Send failed — check the chat ID and that the bot has been added/started in that chat.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
