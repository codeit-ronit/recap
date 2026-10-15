"""Tiny SQLite helper shared by the stores (runs, events, approvals, memory, sandbox).

SQLite keeps V1 dependency-free and inspectable: every database is a single file
under data/ that you can open with any SQLite browser.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


def connect(path: Path | str) -> sqlite3.Connection:
    """Open (and create if needed) a SQLite database with sensible defaults."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    # WAL lets the MCP server process and the oracle read the same file concurrently.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn
