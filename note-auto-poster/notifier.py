import logging
from datetime import date

import requests

from config import (
    DISCORD_WEBHOOK_URL,
    LINE_CHANNEL_ACCESS_TOKEN,
    LINE_USER_ID,
)

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT = 10  # seconds
_LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"

# Discord embed の色（16進数 int）
_COLOR_SUCCESS = 0x57F287  # 緑
_COLOR_ERROR = 0xED4245    # 赤


# ---------------------------------------------------------------------------
# パブリック API
# ---------------------------------------------------------------------------

def send_notification(message: str, url: str | None = None) -> None:
    """設定済みの通知チャンネルへ完了通知を送信する。

    DISCORD_WEBHOOK_URL / LINE_CHANNEL_ACCESS_TOKEN + LINE_USER_ID が
    設定されているチャンネルすべてに送信する。
    どちらも未設定の場合はログ出力のみ（エラーにはしない）。

    Args:
        message: 通知本文
        url: 添付するリンク（Notion ページ URL など、省略可）
    """
    _dispatch(
        text=message,
        url=url,
        discord_title="✅ 記事が作成されました",
        discord_color=_COLOR_SUCCESS,
    )


def send_error_notification(error: Exception, step: str) -> None:
    """エラー発生時に設定済みチャンネルへエラー内容を通知する。

    Args:
        error: 発生した例外オブジェクト
        step: エラーが発生したステップ名（例: "fetch", "filter"）
    """
    today = date.today().isoformat()
    text = f"[{today}] ステップ「{step}」でエラーが発生しました。\n{type(error).__name__}: {error}"

    _dispatch(
        text=text,
        url=None,
        discord_title=f"❌ エラー: {step}",
        discord_color=_COLOR_ERROR,
    )


# ---------------------------------------------------------------------------
# 内部ディスパッチャ
# ---------------------------------------------------------------------------

def _dispatch(
    text: str,
    url: str | None,
    discord_title: str,
    discord_color: int,
) -> None:
    """Discord と LINE の両方（設定済みのもの）に通知を送る。"""
    notified = False

    if DISCORD_WEBHOOK_URL:
        notify_discord(text, url=url, title=discord_title, color=discord_color)
        notified = True

    if LINE_CHANNEL_ACCESS_TOKEN and LINE_USER_ID:
        line_text = f"{discord_title}\n{text}"
        if url:
            line_text += f"\n{url}"
        notify_line(line_text)
        notified = True

    if not notified:
        logger.info(f"[通知先なし] {discord_title} | {text}" + (f" | {url}" if url else ""))


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------

def notify_discord(
    text: str,
    url: str | None = None,
    title: str = "通知",
    color: int = _COLOR_SUCCESS,
) -> None:
    """Discord Webhook に embed 形式でメッセージを送信する。

    DISCORD_WEBHOOK_URL が未設定の場合はスキップする。

    Args:
        text: embed の description に表示するテキスト
        url: embed のタイトルリンク（省略可）
        title: embed のタイトル文字列
        color: embed の左側ライン色（10進数 int）
    """
    if not DISCORD_WEBHOOK_URL:
        logger.debug("DISCORD_WEBHOOK_URL 未設定のため Discord 通知をスキップ")
        return

    embed: dict = {
        "title": title,
        "description": text,
        "color": color,
        "footer": {"text": "note-auto-poster"},
    }
    if url:
        embed["url"] = url

    payload = {"embeds": [embed]}

    try:
        resp = requests.post(
            DISCORD_WEBHOOK_URL,
            json=payload,
            timeout=_REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        logger.info("Discord 通知送信完了")
    except requests.RequestException as e:
        logger.error(f"Discord 通知失敗: {e}")


# ---------------------------------------------------------------------------
# LINE
# ---------------------------------------------------------------------------

def notify_line(text: str) -> None:
    """LINE Messaging API の Push Message でテキストを送信する。

    LINE_CHANNEL_ACCESS_TOKEN または LINE_USER_ID が未設定の場合はスキップする。

    Args:
        text: 送信するテキスト（最大 5000 文字、超過分は切り詰め）
    """
    if not LINE_CHANNEL_ACCESS_TOKEN:
        logger.debug("LINE_CHANNEL_ACCESS_TOKEN 未設定のため LINE 通知をスキップ")
        return
    if not LINE_USER_ID:
        logger.debug("LINE_USER_ID 未設定のため LINE 通知をスキップ")
        return

    # LINE テキストメッセージは 5000 文字上限
    truncated = text[:4997] + "…" if len(text) > 5000 else text

    payload = {
        "to": LINE_USER_ID,
        "messages": [{"type": "text", "text": truncated}],
    }
    headers = {
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }

    try:
        resp = requests.post(
            _LINE_PUSH_URL,
            json=payload,
            headers=headers,
            timeout=_REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        logger.info("LINE 通知送信完了")
    except requests.RequestException as e:
        logger.error(f"LINE 通知失敗: {e}")
