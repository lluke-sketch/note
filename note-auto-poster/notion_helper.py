import logging
import re
from datetime import date, timedelta
from typing import Any

from notion_client import Client

try:
    from martian import convert as _martian_convert
    _MARTIAN_AVAILABLE = True
except ImportError:
    _MARTIAN_AVAILABLE = False

from config import NOTION_API_KEY, NOTION_DATABASE_ID

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


def _extract_source_urls(article: str) -> list[str]:
    """「参照ソース / 元ソース / References」セクション内の URL を収集する。"""
    urls: list[str] = []
    in_section = False
    section_re = re.compile(r'^#{1,3}\s*(参照ソース|元ソース|References|参考リンク)', re.IGNORECASE)
    next_section_re = re.compile(r'^#{1,3}\s+')
    url_re = re.compile(r'https?://[^\s\)>]+')

    for line in article.splitlines():
        stripped = line.strip()
        if section_re.match(stripped):
            in_section = True
            continue
        if in_section:
            # 次のセクション見出しに入ったら終了
            if next_section_re.match(stripped) and not section_re.match(stripped):
                break
            m = url_re.search(stripped)
            if m:
                urls.append(m.group())

    return urls


def _extract_hashtags(article: str) -> str:
    """末尾付近の「#タグ #タグ」形式の行を返す。見つからなければ空文字。"""
    hashtag_line_re = re.compile(r'^(#[^\s#]+)(\s+#[^\s#]+)*\s*$')
    for line in reversed(article.splitlines()):
        stripped = line.strip()
        if hashtag_line_re.match(stripped):
            return stripped
    return ""


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


def save_draft(
    article: str,
    title: str = "",
    source_urls: list[str] | None = None,
    hashtags: str = "",
) -> str:
    """Notion データベースに記事を下書きとして保存し、ページ URL を返す。

    プロパティ:
        - title (title): 記事タイトル
        - ステータス (select): "下書き"
        - 生成日 (date): 今日の日付
        - 元ソース (rich_text): 参照 URL をカンマ区切り
        - ハッシュタグ (rich_text): ハッシュタグ文字列

    ページ本文は Markdown を Notion ブロックに変換して挿入する。
    Notion の 100 ブロック/リクエスト制限に対応するため分割して送信する。

    Args:
        article: Markdown 形式の記事文字列
        title: 記事タイトル（省略時は article の最初の # 見出しから抽出）
        source_urls: 参照 URL リスト（省略時は article から抽出）
        hashtags: ハッシュタグ文字列（省略時は article から抽出）

    Returns:
        str: 作成された Notion ページの URL
    """
    client = _get_client()

    if not title:
        title = _extract_title(article)
    if source_urls is None:
        source_urls = _extract_source_urls(article)
    if not hashtags:
        hashtags = _extract_hashtags(article)

    page = client.pages.create(
        parent={"database_id": NOTION_DATABASE_ID},
        properties={
            "title": {
                "title": [{"type": "text", "text": {"content": title}}]
            },
            "ステータス": {
                "select": {"name": "下書き"}
            },
            "生成日": {
                "date": {"start": date.today().isoformat()}
            },
            "元ソース": {
                "rich_text": [{"type": "text", "text": {"content": ", ".join(source_urls)}}]
            },
            "ハッシュタグ": {
                "rich_text": [{"type": "text", "text": {"content": hashtags}}]
            },
        },
    )

    page_id: str = page["id"]

    blocks = markdown_to_blocks(article)
    for i in range(0, len(blocks), _BLOCKS_PER_REQUEST):
        client.blocks.children.append(
            block_id=page_id,
            children=blocks[i : i + _BLOCKS_PER_REQUEST],
        )

    logger.info(f"Notion ページ作成完了: {page_id} ({len(blocks)} ブロック)")
    return get_page_url(page_id)


def get_recent_urls(days: int = 7) -> list[str]:
    """過去 N 日間に保存した記事の元ソース URL リストを返す（重複なし）。

    Args:
        days: 遡る日数（デフォルト: 7）

    Returns:
        list[str]: 元ソース URL リスト
    """
    client = _get_client()
    since = (date.today() - timedelta(days=days)).isoformat()

    pages: list[dict[str, Any]] = []
    cursor: str | None = None

    while True:
        kwargs: dict[str, Any] = {
            "database_id": NOTION_DATABASE_ID,
            "filter": {"property": "生成日", "date": {"on_or_after": since}},
            "page_size": 100,
        }
        if cursor:
            kwargs["start_cursor"] = cursor

        resp = client.databases.query(**kwargs)
        pages.extend(resp.get("results", []))

        if not resp.get("has_more"):
            break
        cursor = resp.get("next_cursor")

    seen: set[str] = set()
    urls: list[str] = []
    for page in pages:
        raw = (
            page.get("properties", {})
            .get("元ソース", {})
            .get("rich_text", [{}])[0]
            .get("text", {})
            .get("content", "")
        )
        for url in raw.split(","):
            url = url.strip()
            if url and url not in seen:
                seen.add(url)
                urls.append(url)

    return urls


def get_recent_articles(count: int = 3) -> list[dict[str, Any]]:
    """直近 N 件の記事を取得する。「投稿済」を優先し、不足分を「下書き」で補完する。

    Args:
        count: 取得件数（デフォルト: 3）

    Returns:
        list[dict]: 各要素は {"title": str, "url": str, "body": str, "status": str}
    """
    client = _get_client()

    posted = _query_by_status(client, "投稿済", count)
    remaining = count - len(posted)
    drafts = _query_by_status(client, "下書き", remaining) if remaining > 0 else []
    pages = (posted + drafts)[:count]

    articles = []
    for page in pages:
        page_id: str = page["id"]

        title_items = page.get("properties", {}).get("title", {}).get("title", [])
        title = title_items[0].get("text", {}).get("content", "") if title_items else ""

        select = page.get("properties", {}).get("ステータス", {}).get("select") or {}
        status = select.get("name", "")

        articles.append({
            "title": title,
            "url": get_page_url(page_id),
            "body": _fetch_body(client, page_id),
            "status": status,
        })

    return articles


def _query_by_status(client: Client, status: str, limit: int) -> list[dict[str, Any]]:
    """指定ステータスのページを新しい順に最大 limit 件取得する。"""
    if limit <= 0:
        return []
    resp = client.databases.query(
        database_id=NOTION_DATABASE_ID,
        filter={"property": "ステータス", "select": {"equals": status}},
        sorts=[{"property": "生成日", "direction": "descending"}],
        page_size=min(limit, 100),
    )
    return resp.get("results", [])[:limit]


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
