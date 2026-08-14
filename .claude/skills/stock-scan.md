Scan enabled stocks and report which human-review conditions are triggered.

Usage: /stock-scan

You are an evidence reviewer. Your job is to:

1. Load all tickers from `signals/rules.yaml`
2. Fetch current data for each
3. Run the signal engine in `signals/engine.py`
4. Present a clean dashboard of triggered, not-triggered, unknown, and conflicting evaluations

Output format:
```
🔴 Downside / exit review conditions:
  • [ticker] [name] @ [price] — [actual] [operator] [threshold] — [source, as-of]

🟢 Entry review conditions:
  • [ticker] [name] @ [price] — [rule evidence] — [source, as-of]

⚪ No review condition:
  • [ticker] [name] @ [price] — watching

🟡 Could not evaluate:
  • [ticker] [name] — [missing/stale/invalid evidence]

🟠 Conflicting rules:
  • [ticker] [name] — manual policy review required; no directional message emitted
```

Do not infer a market top, dip opportunity, expected return, or trade recommendation from the number of triggered rules. End with a short verification checklist and state that no trade was executed.

Run this Python code:
```python
import sys, yaml

sys.path.insert(0, ".")
from data.fetcher import get_stock
from signals.engine import evaluate

with open("signals/rules.yaml") as f:
    watchlist = yaml.safe_load(f).get("watchlist", {})

for ticker, cfg in watchlist.items():
    try:
        stock = get_stock(ticker, cfg.get("market", "auto"))
        result = evaluate(ticker, stock)
        print(result)
    except Exception as e:
        print(f"{ticker}: ERROR - {e}")
```
