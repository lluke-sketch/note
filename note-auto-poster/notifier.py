import logging

import requests

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT = 10  # seconds
_TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


# ---------------------------------------------------------------------------
# パブリック API
# ---------------------------------------------------------------------------

def send_notification(message: str, url: str | None = None) -> None:
    """Telegram へ完了通知を送信する。

    TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID が未設定の場合はログ出力のみ。

    Args:
        message: 通知本文
        url: 添付する URL（Notion ページなど、省略可）
    """
    text = f"📝 今日のドラフトが完成しました"
    if url:
        text += f"\n{url}"
    if message:
        text += f"\n{message}"

    _send(text)


def send_error_notification(error: Exception, step: str) -> None:
    """Telegram へエラー通知を送信する。

    Args:
        error: 発生した例外オブジェクト
        step: エラーが発生したステップ名（例: "fetch", "filter"）
    """
    text = f"⚠️ エラーが発生しました [{step}]\n{type(error).__name__}: {error}"
    _send(text)


# ---------------------------------------------------------------------------
# 内部送信
# ---------------------------------------------------------------------------

def _send(text: str) -> None:
    """Telegram Bot API の sendMessage を呼び出す。

    TOKEN / CHAT_ID が未設定の場合はログ出力のみでエラーにしない。
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.info(f"[通知先なし] {text}")
        return

    url = _TELEGRAM_API.format(token=TELEGRAM_BOT_TOKEN)
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
    }

    try:
        resp = requests.post(url, json=payload, timeout=_REQUEST_TIMEOUT)
        resp.raise_for_status()
        logger.info("Telegram 通知送信完了")
    except requests.RequestException as e:
        logger.error(f"Telegram 通知失敗: {e}")
