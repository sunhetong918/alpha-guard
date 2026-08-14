"""Market-data adapters and normalized snapshots."""

from .fetcher import canonical_hk_symbol, get_hk_stock, get_stock, get_us_stock

__all__ = ["canonical_hk_symbol", "get_hk_stock", "get_stock", "get_us_stock"]
