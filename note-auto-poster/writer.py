import logging
import re
from datetime import date
from typing import Any

import anthropic

from config import ANTHROPIC_API_KEY

logger = logging.getLogger(__name__)

_MODEL = "claude-sonnet-4-5"

# プロンプトキャッシュ対象（Sonnet は 1024 トークン以上で有効）
_SYSTEM_PROMPT = """\
あなたは「AI・投資トレンドウォッチャー」として note に記事を書く、\
テクノロジーと投資の両方に精通したライターである。

【出力に関する絶対的制約（厳守）】
マークダウン記法は一切使用しないこと。
見出し（# ## ###）・箇条書き（- ・）・太字（**テキスト**）・水平線（--- ===）は禁止。
すべて平文の段落で書くこと。
文体はだである調で統一すること。「〜だ」「〜である」「〜だろう」で終わること。
「です・ます」は使わない。
「筆者の視点」「まとめ」などのラベルを使わない。
AIっぽい定型フレーズを使わない。

【ペルソナ】
読者は日本在住の30〜50代・投資に関心があるビジネスパーソンである。
難解な専門用語には簡単な補足を添えること。
中立・冷静な視点で、過度な断言を避け「〜の可能性がある」「注目される」などの表現を使うこと。

【記事フォーマット（厳守）】
コードフェンスは使わないこと。

1行目にタイトルのみ記載する（例: 今日のAI・投資ニュース〈2025年5月1日〉）。

以降は各ニュースを段落で書く。各ニュースは3〜5文で構成し、\
事実の要約と投資家目線のコメントを続けて書くこと。
英語記事の場合は日本語に意訳すること。
各ニュース末尾に元記事URLを記載すること（例: 元記事: https://...）。
各ニュースの間は1行空けるだけ。

最後の行にハッシュタグを記載すること。\
#AI #投資 #テクノロジー に加え、記事内容に合った追加タグを2〜4個、\
すべて1行にスペース区切りで記載する。

【品質ガイドライン】
- 各ニュースの要約は元の「概要」と「重要性の理由」を参考にしつつ、\
  読者に価値あるコンテキストを加えて書き直すこと
- 投資家目線のコメントはポジショントーク・根拠のない断言を避け、\
  投資家が考慮すべき視点として提示すること
- ハッシュタグはすべて # で始まり、スペースなし・日本語・英語どちらでも可
"""


def build_prompt(filtered_news: list[dict[str, Any]]) -> str:
    """記事生成用のユーザーメッセージを構築する。

    各ニュースのタイトル・URL・概要・スコア理由をセクション形式で並べ、
    Claude が構造を把握しやすいように整形する。

    Args:
        filtered_news: filter_news() が返す選別済み記事リスト

    Returns:
        str: Claude API の user メッセージ文字列
    """
    d = date.today()
    today = f"{d.year}年{d.month}月{d.day}日"
    lines: list[str] = [
        f"今日の日付: {today}",
        f"記事数: {len(filtered_news)}件",
        "",
        "以下のニュースをもとに記事を作成してください。",
        "",
    ]

    for i, article in enumerate(filtered_news, start=1):
        title = article.get("title", "（タイトルなし）")
        url = article.get("url", "")
        summary = article.get("summary", "")
        reason = article.get("reason", "")
        source = article.get("source", "")

        lines += [
            f"--- ニュース {i} ---",
            f"タイトル: {title}",
            f"URL: {url}",
            f"ソース: {source}",
            f"概要: {summary}",
        ]
        if reason:
            lines.append(f"重要性の理由: {reason}")
        lines.append("")

    return "\n".join(lines)


def format_markdown(raw_text: str) -> str:
    """Claude の出力を整形済みテキストとして返す。

    コードフェンス除去・水平線除去・連続空行の圧縮・末尾の整理を行う。
    Claude が正しいフォーマットで出力していれば実質ノーオペレーションになる。

    Args:
        raw_text: Claude API が返した生テキスト

    Returns:
        str: 整形済みテキスト文字列
    """
    text = raw_text.strip()

    # ```markdown ... ``` または ``` ... ``` で囲まれていれば除去
    text = re.sub(r"^```(?:markdown)?\s*\n", "", text)
    text = re.sub(r"\n```\s*$", "", text)
    text = text.strip()

    # 誤って出力された水平線を除去
    text = re.sub(r"\n-{3,}\n", "\n\n", text)
    text = re.sub(r"\n={3,}\n", "\n\n", text)

    # 3行以上連続する空行を2行に圧縮
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text


def write_article(filtered_news: list[dict[str, Any]]) -> str:
    """選別済みニュースをもとに note 向け Markdown 記事を生成する。

    Claude Sonnet に1回のリクエストで全ニュースを渡し、
    指定フォーマット通りの記事を生成させる。
    システムプロンプトにはキャッシュを適用してコストを抑える。

    Args:
        filtered_news: filter_news() が返す選別済み記事リスト

    Returns:
        str: Markdown 形式の記事文字列
    """
    if not filtered_news:
        raise ValueError("filtered_news が空です。記事を生成できません。")

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    user_message = build_prompt(filtered_news)

    logger.info(f"Claude Sonnet に記事生成リクエスト: {len(filtered_news)}件のニュース")

    response = client.messages.create(
        model=_MODEL,
        max_tokens=4096,
        system=[
            {
                "type": "text",
                "text": _SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": user_message}],
    )

    raw = response.content[0].text
    logger.info(
        f"記事生成完了: {response.usage.output_tokens} output tokens "
        f"(cache_read={getattr(response.usage, 'cache_read_input_tokens', 0)}, "
        f"cache_creation={getattr(response.usage, 'cache_creation_input_tokens', 0)})"
    )

    return format_markdown(raw)
