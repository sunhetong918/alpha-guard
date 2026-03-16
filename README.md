# alpha-guard

A rules-based stock monitoring agent for HK and US markets. Sends Telegram alerts when your buy/sell conditions are met. No auto-trading — you stay in control.

## Philosophy

- **Rules over opinions**: buy/sell triggers are pure math (price, PE, ROE thresholds you set)
- **AI for analysis, not recommendations**: Claude scores fundamentals objectively, never says "buy this"
- **You confirm every trade**: the bot notifies you, you decide, you execute manually

## Structure

```
alpha-guard/
├── data/fetcher.py          # yfinance (US) + akshare (HK) data
├── analysis/scorer.py       # Buffett-style fundamental scoring (100pt)
├── signals/
│   ├── rules.yaml           # your watchlist + buy/sell rules
│   └── engine.py            # rule evaluation logic
├── news/                    # news monitoring module
│   ├── config.yaml          # keywords, macro topics, source settings
│   ├── sources.py           # Finnhub + NewsAPI + akshare news feeds
│   └── filter.py            # keyword matching + Claude AI scoring (1-5)
├── notifier/telegram_bot.py # Telegram alerts (signals + news)
├── main.py                  # scheduler (stocks + news every 4h)
└── .claude/skills/          # Claude Code skills
    ├── stock-analyze.md     # /stock-analyze AAPL
    ├── stock-scan.md        # /stock-scan
    └── stock-rules.md       # /stock-rules AAPL
```

## Setup

```bash
git clone <your-repo>
cd alpha-guard
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# fill in your API keys in .env:
#   TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID
#   FINNHUB_API_KEY   (free: https://finnhub.io/register)
#   NEWSAPI_API_KEY   (free: https://newsapi.org/register)
#   ANTHROPIC_API_KEY
```

### Get your Telegram credentials

1. Message [@BotFather](https://t.me/BotFather) → `/newbot` → copy the token
2. Message your bot, then visit `https://api.telegram.org/bot<TOKEN>/getUpdates` to find your `chat_id`

## Usage

**Run the scheduler** (keeps running, fires at market open times):
```bash
python main.py
```

**Manual scan stocks**:
```bash
python main.py scan
```

**Manual scan news**:
```bash
python main.py news
```

**Claude Code skills** (from inside this repo):
```
/stock-analyze AAPL          # full fundamental scorecard
/stock-analyze 00700 HK      # Hong Kong stock
/stock-scan                  # check all watchlist signals
/stock-rules AAPL            # design buy/sell rules for a stock
```

## Scoring Model (100 points)

| Dimension | Weight | Key Metrics |
|-----------|--------|-------------|
| ROE | 25 | Sustained return on equity |
| Valuation | 25 | PE (TTM) + PB ratio |
| Moat | 20 | Free cash flow + debt/equity |
| Growth | 15 | Revenue + earnings growth YoY |
| Safety margin | 15 | Distance from 52-week low |

## Configuring Rules

Edit `signals/rules.yaml`. Supported rule types:

| Type | Meaning |
|------|---------|
| `price_above` | current price ≥ value |
| `price_below` | current price ≤ value |
| `pe_above` | PE ratio ≥ value |
| `pe_below` | PE ratio ≤ value |
| `roe_above` | ROE% ≥ value |
| `price_drop_pct` | drop from cost_basis ≥ value% (stop loss) |

Sell rules: **any** condition triggers an alert.
Buy rules: **all** conditions must be met.

## News Monitoring

The `news/` module scans financial, political and military news every 4 hours:

1. **Fetch** — pulls from Finnhub (US company + market news), NewsAPI (global), akshare (CN financial)
2. **Match** — keyword filter links articles to your watchlist stocks and macro topics
3. **Score** — Claude AI rates impact 1-5, only alerts ≥ 3 get pushed
4. **Notify** — Telegram message with AI analysis, direction (bullish/bearish), and related holdings

Configure keywords and topics in `news/config.yaml`.

## Roadmap

- [ ] Web UI dashboard (FastAPI + simple HTML)
- [ ] Portfolio P&L tracking
- [ ] Earnings calendar alerts
- [ ] Broker API integration (Futu OpenAPI) for one-tap execution

## Disclaimer

This tool is for personal research only. Nothing here is financial advice.
