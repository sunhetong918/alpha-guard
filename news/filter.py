"""
新闻过滤模块
- 关键词匹配：粗筛，判断新闻是否与持仓 / 宏观主题相关
- AI 评分：细筛，Claude 判断新闻对投资的影响程度（1-5 分）
"""
import os
import re
import logging
from typing import Optional

import anthropic
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")


def match_keywords(
    article: dict,
    stock_keywords: dict,
    macro_topics: list[dict],
) -> dict:
    """
    关键词匹配，返回匹配结果

    返回:
        {
            "matched": True/False,
            "related_tickers": ["AAPL", ...],
            "related_topics": ["货币政策", ...],
        }
    """
    title = article.get("title", "")
    summary = article.get("summary", "")
    text = f"{title} {summary}".lower()

    related_tickers = []
    related_topics = []

    # 匹配个股关键词
    for ticker, kw_groups in stock_keywords.items():
        all_kws = kw_groups.get("en", []) + kw_groups.get("zh", [])
        for kw in all_kws:
            if kw.lower() in text:
                related_tickers.append(ticker)
                break

    # 如果新闻本身就标记了关联股票（来自 Finnhub）
    if article.get("related_ticker"):
        t = article["related_ticker"]
        if t not in related_tickers:
            related_tickers.append(t)

    # 匹配宏观主题
    for topic in macro_topics:
        for kw in topic.get("keywords", []):
            if kw.lower() in text:
                related_topics.append(topic["label"])
                break

    matched = bool(related_tickers or related_topics)
    return {
        "matched": matched,
        "related_tickers": related_tickers,
        "related_topics": related_topics,
    }


def ai_score_article(
    article: dict,
    related_tickers: list[str],
    related_topics: list[str],
    watchlist_names: Optional[dict] = None,
) -> dict:
    """
    用 Claude 给新闻打影响分（1-5 分）+ 一句话分析

    返回:
        {"score": 4, "analysis": "...", "affected_direction": "利空"}
    """
    if not ANTHROPIC_KEY:
        logger.warning("ANTHROPIC_API_KEY 未设置，跳过 AI 评分")
        return {"score": 0, "analysis": "AI 评分不可用", "affected_direction": "未知"}

    watchlist_names = watchlist_names or {}
    tickers_desc = ", ".join(
        f"{t}({watchlist_names.get(t, t)})" for t in related_tickers
    ) or "无直接关联个股"
    topics_desc = ", ".join(related_topics) or "无特定宏观主题"

    prompt = f"""你是一位资深投资分析师。请评估以下新闻对投资者的影响。

【新闻标题】{article.get('title', '')}
【新闻摘要】{article.get('summary', '')}
【来源】{article.get('source', '')}
【关联持仓】{tickers_desc}
【关联主题】{topics_desc}

请用以下 JSON 格式返回（不要包含其他内容）：
{{
  "score": <1-5的整数，1=几乎无影响，3=值得关注，5=重大影响>,
  "analysis": "<一句话分析，50字以内，说清楚对哪些股票/板块有什么影响>",
  "affected_direction": "<利好/利空/中性>"
}}"""

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()

        # 提取 JSON（兼容 Claude 可能加的 markdown 代码块）
        json_match = re.search(r"\{.*\}", text, re.DOTALL)
        if json_match:
            import json
            result = json.loads(json_match.group())
            return {
                "score": int(result.get("score", 0)),
                "analysis": result.get("analysis", ""),
                "affected_direction": result.get("affected_direction", "未知"),
            }
        else:
            logger.warning(f"AI 返回格式异常: {text[:100]}")
            return {"score": 0, "analysis": text[:80], "affected_direction": "未知"}

    except Exception as e:
        logger.error(f"AI 评分失败: {e}")
        return {"score": 0, "analysis": f"评分出错: {e}", "affected_direction": "未知"}


def filter_news(
    articles: list[dict],
    stock_keywords: dict,
    macro_topics: list[dict],
    watchlist_names: Optional[dict] = None,
    alert_threshold: int = 3,
    max_ai_calls: int = 20,
) -> list[dict]:
    """
    完整过滤流水线：关键词粗筛 → AI 评分细筛

    返回通过阈值的新闻列表，每条包含:
        原始字段 + related_tickers, related_topics, ai_score, ai_analysis, affected_direction
    """
    # 第一轮：关键词匹配
    matched = []
    for article in articles:
        result = match_keywords(article, stock_keywords, macro_topics)
        if result["matched"]:
            article["related_tickers"] = result["related_tickers"]
            article["related_topics"] = result["related_topics"]
            matched.append(article)

    logger.info(f"关键词匹配: {len(matched)}/{len(articles)} 条新闻命中")

    # 第二轮：AI 评分（只对匹配到的做，控制调用次数）
    scored = []
    ai_calls = 0

    for article in matched:
        if ai_calls >= max_ai_calls:
            article["ai_score"] = 0
            article["ai_analysis"] = "超出本轮 AI 调用上限，未评分"
            article["affected_direction"] = "未知"
            scored.append(article)
            continue

        ai_result = ai_score_article(
            article,
            article["related_tickers"],
            article["related_topics"],
            watchlist_names,
        )
        ai_calls += 1

        article["ai_score"] = ai_result["score"]
        article["ai_analysis"] = ai_result["analysis"]
        article["affected_direction"] = ai_result["affected_direction"]
        scored.append(article)

    # 第三轮：按阈值过滤
    alerts = [a for a in scored if a.get("ai_score", 0) >= alert_threshold]
    alerts.sort(key=lambda x: x.get("ai_score", 0), reverse=True)

    logger.info(
        f"AI 评分完成: {ai_calls} 次调用，{len(alerts)} 条达到阈值 (>= {alert_threshold})"
    )
    return alerts
