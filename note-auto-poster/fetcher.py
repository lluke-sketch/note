import calendar
import html
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode, urlparse, urlunparse, parse_qsl

import feedparser
import requests

from config import FEEDS, NEWSAPI_KEY

logger = logging.getLogger(__name__)

_SUMMARY_MAX_LEN = 500
_NEWSAPI_ENDPOINT = "https://newsapi.org/v2/everything"
_NEWSAPI_QUERY = "AI OR artificial intelligence OR 投資"
_NEWSAPI_PAGE_SIZE = 100
_NEWSAPI_TIMEOUT = 10  # seconds
_TRACKING_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign",
    "utm_content", "utm_term", "ref", "fbclid", "gclid",
})


# ---------------------------------------------------------------------------
# URL ユーティリティ
# ---------------------------------------------------------------------------

def normalize_url(url: str) -> str:
    """トラッキングパラメータ・フラグメント・末尾スラッシュを除去して URL を正規化する。

    除去対象クエリパラメータ:
        utm_source, utm_medium, utm_campaign, utm_content, utm_term,
        ref, fbclid, gclid

    Args:
        url: 正規化前の URL 文字列

    Returns:
        str: 正規化済み URL
    """
    parsed = urlparse(url.strip())

    # トラッキングパラメータを除去して残りを再構築
    filtered_qs = [
        (k, v) for k, v in parse_qsl(parsed.query)
        if k.lower() not in _TRACKING_PARAMS
    ]
    clean_query = urlencode(filtered_qs)

    # パスの末尾スラッシュを統一（ルート "/" はそのまま残す）
    path = parsed.path.rstrip("/") or "/"

    normalized = urlunparse((
        parsed.scheme,
        parsed.netloc,
        path,
        parsed.params,
        clean_query,
        "",  # fragment を除去
    ))
    return normalized


# ---------------------------------------------------------------------------
# 内部ヘルパー
# ---------------------------------------------------------------------------

def _strip_html(text: str) -> str:
    """HTML タグと実体参照を除去してプレーンテキストを返す。"""
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _truncate(text: str, max_len: int = _SUMMARY_MAX_LEN) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len - 1] + "…"


def _entry_published(entry: Any) -> str:
    """feedparser エントリから ISO 8601 の公開日時文字列を返す。

    published_parsed → updated_parsed → 現在時刻 の順にフォールバックする。
    """
    struct = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if struct:
        dt = datetime.fromtimestamp(calendar.timegm(struct), tz=timezone.utc)
    else:
        dt = datetime.now(tz=timezone.utc)
    return dt.isoformat()


def _entry_summary(entry: Any) -> str:
    """feedparser エントリから概要テキストを取得し、HTML 除去・切り詰めを行う。"""
    raw = (
        getattr(entry, "summary", "")
        or getattr(entry, "description", "")
        or ""
    )
    return _truncate(_strip_html(raw))


# ---------------------------------------------------------------------------
# RSS 取得
# ---------------------------------------------------------------------------

def fetch_rss(feed_urls: list[str]) -> list[dict[str, Any]]:
    """指定された RSS フィード URL リストから記事を取得して正規化する。

    feedparser のパースエラーは記事単位でスキップし、他の記事の取得を続ける。

    Args:
        feed_urls: RSS フィードの URL リスト

    Returns:
        list[dict]: 正規化済み記事リスト
            {"title", "url", "summary", "source", "published"}
    """
    articles: list[dict[str, Any]] = []

    for feed_url in feed_urls:
        try:
            feed = feedparser.parse(feed_url)

            # feedparser はネットワークエラーでも例外を投げず bozo フラグを立てる
            if feed.bozo and not feed.entries:
                logger.warning(f"RSS パース失敗 ({feed_url}): {feed.bozo_exception}")
                continue

            source_name = feed.feed.get("title", urlparse(feed_url).netloc)
            logger.info(f"RSS 取得: {source_name} ({len(feed.entries)}件) - {feed_url}")

            for entry in feed.entries:
                url = normalize_url(entry.get("link", ""))
                if not url:
                    continue

                articles.append({
                    "title": _strip_html(entry.get("title", "")).strip(),
                    "url": url,
                    "summary": _entry_summary(entry),
                    "source": source_name,
                    "published": _entry_published(entry),
                })

        except Exception as e:
            logger.error(f"RSS 取得エラー ({feed_url}): {e}")

    return articles


# ---------------------------------------------------------------------------
# NewsAPI 取得
# ---------------------------------------------------------------------------

def fetch_newsapi(query: str = _NEWSAPI_QUERY, language: str = "en") -> list[dict[str, Any]]:
    """NewsAPI から過去 24 時間の記事を取得して正規化する。

    NEWSAPI_KEY が未設定の場合は空リストを返してスキップする。

    Args:
        query: 検索キーワード（デフォルト: "AI OR artificial intelligence OR 投資"）
        language: 言語コード（"en" または "ja"）

    Returns:
        list[dict]: 正規化済み記事リスト
            {"title", "url", "summary", "source", "published"}
    """
    if not NEWSAPI_KEY:
        logger.info("NEWSAPI_KEY 未設定のため NewsAPI をスキップ")
        return []

    since = (datetime.now(tz=timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")

    params = {
        "q": query,
        "language": language,
        "from": since,
        "sortBy": "publishedAt",
        "pageSize": _NEWSAPI_PAGE_SIZE,
        "apiKey": NEWSAPI_KEY,
    }

    try:
        resp = requests.get(_NEWSAPI_ENDPOINT, params=params, timeout=_NEWSAPI_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        logger.error(f"NewsAPI リクエストエラー (language={language}): {e}")
        return []

    if data.get("status") != "ok":
        logger.error(f"NewsAPI エラーレスポンス: {data.get('message', data)}")
        return []

    articles: list[dict[str, Any]] = []
    for item in data.get("articles", []):
        url = normalize_url(item.get("url", ""))
        if not url:
            continue

        raw_summary = item.get("description") or item.get("content") or ""
        articles.append({
            "title": _strip_html(item.get("title", "")).strip(),
            "url": url,
            "summary": _truncate(_strip_html(raw_summary)),
            "source": item.get("source", {}).get("name", "NewsAPI"),
            "published": item.get("publishedAt", datetime.now(tz=timezone.utc).isoformat()),
        })

    logger.info(f"NewsAPI 取得: {len(articles)}件 (language={language})")
    return articles


# ---------------------------------------------------------------------------
# メイン取得関数
# ---------------------------------------------------------------------------

def fetch_news() -> list[dict[str, Any]]:
    """全ソースからニュースを取得し、正規化・重複排除して返す。

    取得元:
        1. config.FEEDS に定義された RSS フィード
        2. NewsAPI（NEWSAPI_KEY が設定されている場合のみ、en / ja 両言語）

    重複排除は正規化済み URL をキーとする。

    Returns:
        list[dict]: 重複なし・正規化済みの記事リスト
            {"title", "url", "summary", "source", "published"}
    """
    all_articles: list[dict[str, Any]] = []

    # RSS
    all_articles.extend(fetch_rss(FEEDS))

    # NewsAPI（英語・日本語）
    for lang in ("en", "ja"):
        all_articles.extend(fetch_newsapi(language=lang))

    # URL ベースの重複排除（先に取得したものを優先）
    seen_urls: set[str] = set()
    unique: list[dict[str, Any]] = []
    for article in all_articles:
        url = article["url"]
        if url and url not in seen_urls:
            seen_urls.add(url)
            unique.append(article)

    logger.info(f"取得合計: {len(all_articles)}件 → 重複排除後: {len(unique)}件")
    return unique
