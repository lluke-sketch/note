import json
import logging
import re
from typing import Any

import anthropic

import notion_helper
from config import ANTHROPIC_API_KEY

logger = logging.getLogger(__name__)

_MODEL = "claude-haiku-4-5-20251001"
_SCORE_THRESHOLD = 80
_MAX_ARTICLES = 5

# キャッシュ対象のシステムプロンプト（1024 トークン以上でキャッシュが有効になる）
_SYSTEM_PROMPT = """\
あなたは投資・テクノロジーに精通したニュースキュレーターです。
提供されたニュース記事リストを、以下の3つの基準の総合評価として 0〜100 点でスコアリングしてください。

【評価基準】
1. 投資判断への影響度
   - 市場動向・企業業績・規制変更・金融政策など、投資家の意思決定に直結する情報かどうか
   - 株価・為替・コモディティへの波及可能性

2. AI業界における重要度
   - 技術的ブレークスルー、主要モデル/プロダクトのリリース、大手プレイヤーの戦略変更
   - オープンソース動向、研究論文の実用化、規制・倫理面の進展

3. 日本の読者にとっての関心度
   - 日本市場・日本企業・日本語圏ユーザーへの直接的影響
   - 日本のビジネスパーソンが知っておくべき国際情勢

【出力形式】
入力された JSON 配列のインデックス順を保ちつつ、必ず以下の JSON 配列のみを返してください。
余分なテキスト・Markdown コードフェンスは含めないこと。

[
  {"index": <元の index 値>, "score": <0〜100 の整数>, "reason": "<50字以内の日本語評価理由>"},
  ...
]
"""


def filter_news(news_list: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """ニュースリストから重要ニュースを最大5件（最低1件）選別して返す。

    処理フロー:
      1. 過去7日間の投稿済み URL と重複する記事を除外
      2. Claude Haiku でバッチスコアリング
      3. 80点以上を抽出、スコア降順で最大5件に絞る
      4. 80点以上が0件の場合は最高スコアの1件を返す

    Args:
        news_list: fetch_news() が返す記事リスト

    Returns:
        list[dict]: 選別された記事リスト（各記事に "score", "reason" キー追加）
    """
    if not news_list:
        return []

    deduped = deduplicate(news_list)
    if not deduped:
        logger.info("重複除外後に記事が残らなかったため空リストを返します")
        return []

    scored = score_with_claude(deduped)
    scored.sort(key=lambda a: a.get("score", 0), reverse=True)

    above_threshold = [a for a in scored if a.get("score", 0) >= _SCORE_THRESHOLD]

    if above_threshold:
        result = above_threshold[:_MAX_ARTICLES]
    else:
        # しきい値を超える記事がなければ最高スコアの1件を強制採用
        logger.warning(
            f"{_SCORE_THRESHOLD}点以上の記事なし。最高スコア記事を1件採用: "
            f"score={scored[0].get('score')}"
        )
        result = scored[:1]

    logger.info(f"選別完了: {len(result)}件 / 入力{len(news_list)}件")
    return result


def deduplicate(news_list: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """過去7日間の投稿済み URL との重複と、バッチ内の URL 重複を除去する。

    Notion へのアクセスに失敗した場合はスキップしてバッチ内重複のみ処理する。

    Args:
        news_list: 重複を含む可能性がある記事リスト

    Returns:
        list[dict]: 重複を除去した記事リスト
    """
    # 過去投稿済み URL を取得（失敗してもパイプラインを止めない）
    recent_urls: set[str] = set()
    try:
        recent_urls = set(notion_helper.get_recent_urls(days=7))
        logger.info(f"過去7日間の投稿済み URL: {len(recent_urls)}件")
    except Exception as e:
        logger.warning(f"Notion から過去 URL の取得に失敗（重複チェックをスキップ）: {e}")

    seen_urls: set[str] = set()
    result: list[dict[str, Any]] = []

    for article in news_list:
        url = article.get("url", "")

        if url and url in recent_urls:
            logger.debug(f"投稿済み重複スキップ: {url}")
            continue
        if url and url in seen_urls:
            logger.debug(f"バッチ内重複スキップ: {url}")
            continue

        if url:
            seen_urls.add(url)
        result.append(article)

    logger.info(f"重複除外: {len(news_list)}件 → {len(result)}件")
    return result


def score_with_claude(news_list: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Claude Haiku へバッチリクエストを送り、各記事にスコアと理由を付与する。

    全記事を JSON 配列で一度に渡すことでトークン消費を抑える。
    JSON パースに失敗した場合はフォールバックスコア (50) を付与する。

    Args:
        news_list: スコアリング対象の記事リスト

    Returns:
        list[dict]: "score" と "reason" キーが追加された記事リスト
    """
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # Claude に渡す記事ペイロード（index を持たせて対応関係を保証する）
    payload = [
        {"index": i, "title": a.get("title", ""), "summary": a.get("summary", "")}
        for i, a in enumerate(news_list)
    ]

    user_message = (
        "以下のニュース記事リストをスコアリングしてください。\n\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
    )

    logger.info(f"Claude Haiku にスコアリングリクエスト: {len(news_list)}件")

    response = client.messages.create(
        model=_MODEL,
        max_tokens=1024,
        system=[
            {
                "type": "text",
                "text": _SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},  # プロンプトキャッシュ
            }
        ],
        messages=[{"role": "user", "content": user_message}],
    )

    raw = response.content[0].text
    logger.debug(f"Claude レスポンス (先頭200字): {raw[:200]}")

    scores = _parse_score_response(raw, len(news_list))

    # スコアをマージ（index で対応付け）
    result = [dict(a) for a in news_list]
    for entry in scores:
        idx = entry.get("index")
        if isinstance(idx, int) and 0 <= idx < len(result):
            result[idx]["score"] = int(entry.get("score", 50))
            result[idx]["reason"] = entry.get("reason", "")

    # スコアが付かなかった記事にデフォルト値を補填
    for article in result:
        if "score" not in article:
            logger.warning(f"スコア未取得のためデフォルト適用: {article.get('title', '')[:40]}")
            article["score"] = 50
            article["reason"] = ""

    return result


def _parse_score_response(raw: str, expected_count: int) -> list[dict[str, Any]]:
    """Claude のレスポンスから JSON 配列を抽出してパースする。

    コードフェンスや前後のテキストが含まれていても対応できるよう
    正規表現でブラケット部分だけを取り出す。
    """
    # コードフェンスを除去
    cleaned = re.sub(r"```(?:json)?\s*", "", raw).strip()

    # 配列部分を抽出（最初の [ から対応する ] まで）
    m = re.search(r"\[.*\]", cleaned, re.DOTALL)
    if not m:
        logger.error(f"JSON 配列が見つかりません。フォールバックスコアを使用: {raw[:200]}")
        return []

    try:
        parsed = json.loads(m.group())
        if not isinstance(parsed, list):
            raise ValueError("レスポンスがリスト形式ではありません")
        return parsed
    except (json.JSONDecodeError, ValueError) as e:
        logger.error(f"JSON パース失敗: {e} / 入力: {m.group()[:200]}")
        return []
