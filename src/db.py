"""
db.py — SQLite helpers compartidos para el whatsapp-assistant.
"""
import sqlite3
from pathlib import Path


def get_connection(db_path: str = "data/whatsapp.db") -> sqlite3.Connection:
    p = Path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS messages (
            message_id   TEXT PRIMARY KEY,
            line         TEXT NOT NULL,
            chat_ref     TEXT NOT NULL,
            sender       TEXT,
            timestamp    TEXT NOT NULL,
            type         TEXT,
            content      TEXT,
            media_ref    TEXT,
            media_path   TEXT,
            quoted_id    TEXT,
            source       TEXT DEFAULT 'export',
            needs_review INTEGER DEFAULT 0,
            created_at   TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_messages_line_ts
            ON messages (line, timestamp DESC);
        CREATE INDEX IF NOT EXISTS idx_messages_chat_ref
            ON messages (chat_ref);
    """)
    conn.commit()
    # Migration para DBs existentes que no tienen media_path
    try:
        conn.execute("ALTER TABLE messages ADD COLUMN media_path TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # columna ya existe
