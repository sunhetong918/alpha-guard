Scan all stocks in the watchlist and report which signals are triggered.

Usage: /stock-scan

You are a systematic quantitative analyst. Your job is to:

1. Load all tickers from `signals/rules.yaml`
2. Fetch current data for each
3. Run the signal engine in `signals/engine.py`
4. Present a clean dashboard of what's triggered

Output format:
```
🔴 SELL signals triggered:
  • [ticker] [name] @ [price] — [reason]

🟢 BUY signals triggered:
  • [ticker] [name] @ [price] — all conditions met

⚪ No signal:
  • [ticker] [name] @ [price] — watching
```

Then give a brief market context comment: are multiple sell signals firing at once (possible broad market top)? Are buy signals appearing (possible dip opportunity)?

Run this Python code:
```python
import sys, yaml
sys.path.insert(0, '.')
from data.fetcher import get_stock
from signals.engine import evaluate

with open('signals/rules.yaml') as f:
    watchlist = yaml.safe_load(f).get('watchlist', {})

for ticker, cfg in watchlist.items():
    try:
        stock = get_stock(ticker, cfg.get('market', 'auto'))
        result = evaluate(ticker, stock)
        print(result)
    except Exception as e:
        print(f"{ticker}: ERROR - {e}")
```
