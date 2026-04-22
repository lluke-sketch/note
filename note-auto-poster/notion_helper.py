import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

from notion_client import Client

try:
    from martian import convert as _martian_convert
    _MARTIAN_AVAILABLE = True
except ImportError:
    _MARTIAN_AVAILABLE = False

from config import NOTION_API_KEY, NOTION_PAGE_ID

logger = logging.getLogger(__name__)

_client: Client | None = None
_NOTION_BASE_URL = "https://www.notion.so"
_BLOCKS_PER_REQUEST = 100  # Notion API の1リクエスト上限


def _get_client() -> Client:
    global _client
    if _client is None:
        if not NOTION_API_KEY:
            raise ValueError("NOTION_API_KEY が設定されていません")
        _client = Client(auth=NOTION_API_KEY)
    return _client


# ---------------------------------------------------------------------------
# Markdown パース ユーティリティ
# ---------------------------------------------------------------------------

def _extract_title(article: str) -> str:
    """最初の # 見出し行からタイトルを返す。見つからなければ日付を使う。"""
    for line in article.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return f"記事 {date.today().isoformat()}"


# ---------------------------------------------------------------------------
# Markdown → Notion ブロック変換
# ---------------------------------------------------------------------------

def markdown_to_blocks(markdown: str) -> list[dict[str, Any]]:
    """Markdown 文字列を Notion ブロックリストに変換する。

    martian が利用可能な場合はそれを優先し、失敗時は
    paragraph ブロックへのフォールバック変換を行う。

    Args:
        markdown: Markdown 形式の文字列

    Returns:
        list[dict]: Notion API ブロックオブジェクトリスト
    """
    if _MARTIAN_AVAILABLE:
        try:
            blocks = _martian_convert(markdown)
            if blocks:
                return blocks
        except Exception as e:
            logger.warning(f"martian 変換失敗、フォールバックへ切り替え: {e}")

    return _fallback_to_blocks(markdown)


def _rich_text(content: str) -> list[dict[str, Any]]:
    """Notion rich_text オブジェクトを生成する（2000 字上限を考慮）。"""
    return [{"type": "text", "text": {"content": content[:2000]}}]


def _paragraph_block(text: str) -> dict[str, Any]:
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": _rich_text(text)}}


def _fallback_to_blocks(markdown: str) -> list[dict[str, Any]]:
    """martian が使えない場合のシンプルなブロック変換。

    対応: # 見出し1-3 / - * 箇条書き / 1. 番号付きリスト / その他は paragraph
    """
    blocks: list[dict[str, Any]] = []
    paragraph_buf: list[str] = []

    def flush_paragraph() -> None:
        if paragraph_buf:
            blocks.append(_paragraph_block(" ".join(paragraph_buf)))
            paragraph_buf.clear()

    for line in markdown.splitlines():
        stripped = line.strip()

        if not stripped:
            flush_paragraph()
            continue

        # 見出し (h1–h3)
        m = re.match(r'^(#{1,3})\s+(.+)', stripped)
        if m:
            flush_paragraph()
            level = min(len(m.group(1)), 3)
            btype = f"heading_{level}"
            blocks.append({
                "object": "block",
                "type": btype,
                btype: {"rich_text": _rich_text(m.group(2))},
            })
            continue

        # 箇条書き
        m = re.match(r'^[-*]\s+(.+)', stripped)
        if m:
            flush_paragraph()
            blocks.append({
                "object": "block",
                "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": _rich_text(m.group(1))},
            })
            continue

        # 番号付きリスト
        m = re.match(r'^\d+\.\s+(.+)', stripped)
        if m:
            flush_paragraph()
            blocks.append({
                "object": "block",
                "type": "numbered_list_item",
                "numbered_list_item": {"rich_text": _rich_text(m.group(1))},
            })
            continue

        paragraph_buf.append(stripped)

    flush_paragraph()
    return blocks


# ---------------------------------------------------------------------------
# 内部ヘルパー
# ---------------------------------------------------------------------------

def _list_child_pages(client: Client) -> list[dict[str, Any]]:
    """親ページ直下の child_page ブロック一覧をすべて返す。"""
    if not NOTION_PAGE_ID:
        return []

    results: list[dict[str, Any]] = []
    cursor: str | None = None

    while True:
        kwargs: dict[str, Any] = {"block_id": NOTION_PAGE_ID, "page_size": 100}
        if cursor:
            kwargs["start_cursor"] = cursor

        resp = client.blocks.children.list(**kwargs)
        for block in resp.get("results", []):
            if block.get("type") == "child_page":
                results.append(block)

        if not resp.get("has_more"):
            break
        cursor = resp.get("next_cursor")

    return results


def _get_page_created_time(client: Client, page_id: str) -> datetime | None:
    """ページの作成日時を UTC aware datetime で返す。取得失敗時は None。"""
    try:
        page = client.pages.retrieve(page_id)
        created_str: str = page.get("created_time", "")
        if created_str:
            return datetime.fromisoformat(created_str.replace("Z", "+00:00"))
    except Exception as e:
        logger.warning(f"ページ作成日時取得失敗 ({page_id}): {e}")
    return None


def _extract_urls_from_blocks(blocks: list[dict[str, Any]]) -> list[str]:
    """Notion ブロックの rich_text から href を収集して URL リストを返す。

    Notion は Markdown リンク [text](url) を rich_text オブジェクトの
    href フィールドに URL を持つ形式で保存する。
    """
    urls: list[str] = []
    for block in blocks:
        btype = block.get("type", "")
        for rt in block.get(btype, {}).get("rich_text", []):
            # href はトップレベルに置かれる場合と text.link.url に置かれる場合がある
            href = rt.get("href") or (rt.get("text", {}).get("link") or {}).get("url", "")
            if href and isinstance(href, str):
                urls.append(href)
    return urls


def _fetch_body(client: Client, page_id: str) -> str:
    """ページのブロックをプレーンテキストとして結合して返す。

    Note: 100 ブロックを超えるページはページネーションを実装していないため
    先頭 100 ブロック分のみ取得する。
    """
    try:
        resp = client.blocks.children.list(block_id=page_id, page_size=100)
        lines = [_block_to_text(b) for b in resp.get("results", [])]
        return "\n".join(line for line in lines if line)
    except Exception as e:
        logger.warning(f"ページ本文取得失敗 ({page_id}): {e}")
        return ""


def _block_to_text(block: dict[str, Any]) -> str:
    """ブロックオブジェクトから plain_text を連結して返す。"""
    btype = block.get("type", "")
    rich_texts = block.get(btype, {}).get("rich_text", [])
    return "".join(rt.get("plain_text", "") for rt in rich_texts)


# ---------------------------------------------------------------------------
# パブリック API
# ---------------------------------------------------------------------------

def get_page_url(page_id: str) -> str:
    """Notion ページ ID からブラウザで開ける URL を返す。

    Args:
        page_id: Notion ページ ID（ハイフンあり・なし両対応）

    Returns:
        str: notion.so 形式のページ URL
    """
    return f"{_NOTION_BASE_URL}/{page_id.replace('-', '')}"


def save_draft(article: str, title: str = "") -> str:
    """親ページのサブページとして記事を保存し、ページ URL を返す。

    pages.create の children パラメータで最初の 100 ブロックを一括送信する。
    100 ブロックを超える場合は blocks.children.append で追加する。

    Args:
        article: Markdown 形式の記事文字列
        title: ページタイトル（省略時は article の最初の # 見出しから抽出）

    Returns:
        str: 作成された Notion ページの URL
    """
    client = _get_client()
    if not NOTION_PAGE_ID:
        raise ValueError("NOTION_PAGE_ID が設定されていません")

    if not title:
        title = _extract_title(article)

    blocks = markdown_to_blocks(article)
    first_chunk = blocks[:_BLOCKS_PER_REQUEST]
    remaining = blocks[_BLOCKS_PER_REQUEST:]

    page = client.pages.create(
        parent={"page_id": NOTION_PAGE_ID},
        properties={
            "title": {
                "title": [{"type": "text", "text": {"content": title}}]
            }
        },
        children=first_chunk,
    )

    page_id: str = page["id"]

    for i in range(0, len(remaining), _BLOCKS_PER_REQUEST):
        client.blocks.children.append(
            block_id=page_id,
            children=remaining[i : i + _BLOCKS_PER_REQUEST],
        )

    logger.info(f"Notion サブページ作成完了: {page_id} ({len(blocks)} ブロック)")
    return get_page_url(page_id)


def get_recent_urls(days: int = 7) -> list[str]:
    """過去 N 日間に保存したサブページの元記事 URL リストを返す（重複なし）。

    各サブページのブロックを取得し、rich_text の href から URL を収集する。

    Args:
        days: 遡る日数（デフォルト: 7）

    Returns:
        list[str]: URL リスト
    """
    client = _get_client()
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=days)

    child_pages = _list_child_pages(client)

    seen: set[str] = set()
    urls: list[str] = []

    for block in child_pages:
        page_id: str = block["id"]

        created = _get_page_created_time(client, page_id)
        if created is not None and created < cutoff:
            continue

        try:
            resp = client.blocks.children.list(block_id=page_id, page_size=100)
            for url in _extract_urls_from_blocks(resp.get("results", [])):
                if url not in seen:
                    seen.add(url)
                    urls.append(url)
        except Exception as e:
            logger.warning(f"ブロック取得失敗 ({page_id}): {e}")

    logger.info(f"過去 {days} 日間の投稿済み URL: {len(urls)} 件")
    return urls


def get_recent_articles(count: int = 3) -> list[dict[str, Any]]:
    """直近 N 件のサブページを作成日時降順で取得する。

    Args:
        count: 取得件数（デフォルト: 3）

    Returns:
        list[dict]: 各要素は {"title": str, "url": str, "body": str}
    """
    client = _get_client()
    child_pages = _list_child_pages(client)

    # 各ページの作成日時を取得してソート
    pages_with_time: list[tuple[datetime, dict[str, Any]]] = []
    for block in child_pages:
        created = _get_page_created_time(client, block["id"])
        if created is not None:
            pages_with_time.append((created, block))

    pages_with_time.sort(key=lambda t: t[0], reverse=True)

    articles = []
    for _, block in pages_with_time[:count]:
        page_id: str = block["id"]
        articles.append({
            "title": block.get("child_page", {}).get("title", ""),
            "url": get_page_url(page_id),
            "body": _fetch_body(client, page_id),
        })

    return articles
