import logging

import fetcher
import filter as news_filter
import writer
import notion_helper as notion_client
import notifier

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def main() -> None:
    """ニュース取得から通知送信までのメインパイプラインを実行する。"""

    # Step 1: ニュース取得
    try:
        logger.info("Step 1: ニュース取得開始")
        news_list = fetcher.fetch_news()
        logger.info(f"取得件数: {len(news_list)}")
    except Exception as e:
        logger.error(f"ニュース取得失敗: {e}")
        notifier.send_error_notification(e, "fetch")
        return

    # Step 2: 重要ニュース選別
    try:
        logger.info("Step 2: ニュース選別開始")
        filtered_news = news_filter.filter_news(news_list)
        logger.info(f"選別後件数: {len(filtered_news)}")
    except Exception as e:
        logger.error(f"ニュース選別失敗: {e}")
        notifier.send_error_notification(e, "filter")
        return

    # Step 3: 記事生成
    try:
        logger.info("Step 3: 記事生成開始")
        article = writer.write_article(filtered_news)
        logger.info("記事生成完了")
    except Exception as e:
        logger.error(f"記事生成失敗: {e}")
        notifier.send_error_notification(e, "write")
        return

    # Step 4: Notionに下書き保存
    try:
        logger.info("Step 4: Notion保存開始")
        notion_url = notion_client.save_draft(article)
        logger.info(f"Notion保存完了: {notion_url}")
    except Exception as e:
        logger.error(f"Notion保存失敗: {e}")
        notifier.send_error_notification(e, "notion_save")
        return

    # Step 5: 通知送信
    try:
        logger.info("Step 5: 通知送信開始")
        notifier.send_notification("下書きを Notion に保存しました。", url=notion_url)
        logger.info("通知送信完了")
    except Exception as e:
        logger.error(f"通知送信失敗: {e}")
        notifier.send_error_notification(e, "notify")


if __name__ == "__main__":
    main()
