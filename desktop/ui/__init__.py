"""Alpha Guard desktop control room.

The UI consumes typed, redacted Guardian receipts.  It never imports the
SQLite store, notification SDKs, or application secrets.
"""

from .app import create_application, run

__all__ = ["create_application", "run"]
