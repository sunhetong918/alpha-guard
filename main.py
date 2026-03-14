"""
主调度脚本 —— 每天开盘前后自动扫描持仓和监控列表
用法：python main.py
"""
import asyncio
import logging
import schedule
import time
import yaml
from pathlib import Path

from data.fetcher import get_stock
from analysis.scorer import analyze, format_report
from signals.engine import evaluate
from notifier.telegram_bot import send_message, send_signal

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

RULES_PATH = Path("signals/rules.yaml")


def load_watchlist() -> dict:
    with open(RULES_PATH) as f:
        return yaml.safe_load(f).get("watchlist", {})


async def scan_all() -> None:
    """扫描所有监控股票，发送触发信号"""
    watchlist = load_watchlist()
    logger.info(f"开始扫描 {len(watchlist)} 只股票...")

    for ticker, cfg in watchlist.items():
        try:
            market = cfg.get("market", "auto")
            stock = get_stock(ticker, market)
            result = evaluate(ticker, stock)

            # 卖出信号
            if result["sell"]:
                await send_signal({
                    "ticker": ticker,
                    "name": result["name"],
                    "price": result["price"],
                    "action": "SELL",
                    "reasons": result["sell"],
                })
                logger.info(f"SELL signal sent: {ticker}")

            # 买入信号
            if result["buy"]:
                await send_signal({
                    "ticker": ticker,
                    "name": result["name"],
                    "price": result["price"],
                    "action": "BUY",
                    "reasons": ["所有买入条件已满足"],
                })
                logger.info(f"BUY signal sent: {ticker}")

        except Exception as e:
            logger.error(f"扫描 {ticker} 失败: {e}")

    logger.info("扫描完成")


async def daily_summary() -> None:
    """每日收盘后发送持仓摘要"""
    watchlist = load_watchlist()
    lines = ["📋 *每日持仓摘要*\n"]

    for ticker, cfg in watchlist.items():
        try:
            market = cfg.get("market", "auto")
            stock = get_stock(ticker, market)
            price = stock.get("price", "N/A")
            cost = cfg.get("cost_basis")
            if cost and price != "N/A":
                pnl = (price - cost) / cost * 100
                pnl_str = f"{'📈' if pnl >= 0 else '📉'} {pnl:+.1f}%"
            else:
                pnl_str = "—"
            lines.append(f"• {cfg.get('name', ticker)} ({ticker}): {price}  {pnl_str}")
        except Exception as e:
            lines.append(f"• {ticker}: 获取失败 ({e})")

    await send_message("\n".join(lines))


def run_schedule() -> None:
    """设置定时任务"""
    # 美股开盘前扫描（北京时间 21:25，美东 9:25）
    schedule.every().monday.at("21:25").do(lambda: asyncio.run(scan_all()))
    schedule.every().tuesday.at("21:25").do(lambda: asyncio.run(scan_all()))
    schedule.every().wednesday.at("21:25").do(lambda: asyncio.run(scan_all()))
    schedule.every().thursday.at("21:25").do(lambda: asyncio.run(scan_all()))
    schedule.every().friday.at("21:25").do(lambda: asyncio.run(scan_all()))

    # 港股开盘前扫描（北京时间 9:25）
    schedule.every().monday.at("09:25").do(lambda: asyncio.run(scan_all()))
    schedule.every().tuesday.at("09:25").do(lambda: asyncio.run(scan_all()))
    schedule.every().wednesday.at("09:25").do(lambda: asyncio.run(scan_all()))
    schedule.every().thursday.at("09:25").do(lambda: asyncio.run(scan_all()))
    schedule.every().friday.at("09:25").do(lambda: asyncio.run(scan_all()))

    # 每日收盘摘要（北京时间 16:10，港股收盘后）
    schedule.every().day.at("16:10").do(lambda: asyncio.run(daily_summary()))

    logger.info("调度器已启动，等待任务触发...")
    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "scan":
        # 手动触发扫描：python main.py scan
        asyncio.run(scan_all())
    else:
        run_schedule()
