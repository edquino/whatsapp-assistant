#!/usr/bin/env python3
"""
parse_export.py — WhatsApp export parser (P1 · Track 2 Piscinas Pacífico)

Parsea un export de WhatsApp (.zip o carpeta extraída) y lo normaliza en
data/whatsapp.db siguiendo el contrato de interfaz con Henry.

Uso:
    python scripts/whatsapp/parse_export.py <ruta_zip_o_carpeta> [--line <nombre>]

Ejemplos:
    python scripts/whatsapp/parse_export.py "projects/piscinas-pacifico/Chat de Whatsapp con Gastos el encanto.zip"
    python scripts/whatsapp/parse_export.py "projects/piscinas-pacifico/Chat de Whatsapp con Piscina ilobasco.zip" --line maurisito
"""

import re
import hashlib
import sqlite3
import zipfile
import shutil
import tempfile
import argparse
from pathlib import Path
from datetime import datetime

# ── Constantes ──────────────────────────────────────────────────────────────

DB_PATH = Path("data/whatsapp.db")

# Formato WhatsApp español: "4/6/2026, 12:55 p. m. - Nombre: mensaje"
LINE_RE = re.compile(
    r'^(\d{1,2}/\d{1,2}/\d{4}),\s*(\d{1,2}:\d{2}\s*[ap]\.\s*m\.)\s*-\s*(.+)$'
)

# Archivos adjuntos: "‎IMG-20260602-WA0031.jpg (archivo adjunto)"
ATTACH_RE = re.compile(r'[‎‏]?(.+?)\s*\(archivo adjunto\)')

# Frases de mensajes del sistema — se descartan
SYSTEM_PHRASES = (
    'cifrados de extremo a extremo',
    'Creaste este grupo',
    'Se añadió a',
    'Se eliminó a',
    'cambió el nombre del grupo',
    'cambió el ícono',
    'cambió la descripción del grupo',
    'Se fue',
    'ahora puedes llamar',
    'Ahora tus mensajes',
    'Los mensajes y llamadas',
)

MEDIA_EXTS: dict[str, str] = {
    '.jpg': 'image', '.jpeg': 'image', '.png': 'image',
    '.webp': 'image', '.gif': 'image',
    '.pdf': 'doc', '.docx': 'doc', '.xlsx': 'doc', '.xls': 'doc',
    '.opus': 'audio', '.ogg': 'audio', '.mp3': 'audio', '.m4a': 'audio', '.aac': 'audio',
    '.mp4': 'video', '.mov': 'video', '.avi': 'video',
}


# ── Base de datos ────────────────────────────────────────────────────────────

def init_db(conn: sqlite3.Connection) -> None:
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


# ── Parsing ──────────────────────────────────────────────────────────────────

def parse_ts(date_str: str, time_str: str) -> str:
    """Convierte 'D/M/YYYY' + 'H:MM a. m.' a ISO timestamp."""
    t = re.sub(r'\s', '', time_str)          # "12:55p.m."
    t = t.replace('p.m.', 'PM').replace('a.m.', 'AM')
    dt = datetime.strptime(f"{date_str} {t}", "%d/%m/%Y %I:%M%p")
    return dt.isoformat()


def make_id(chat_ref: str, sender: str, ts: str, content: str, media_ref: str = '') -> str:
    key = f"{chat_ref}|{sender}|{ts}|{(content or '')[:50]}|{media_ref or ''}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def media_type(filename: str) -> str:
    return MEDIA_EXTS.get(Path(filename).suffix.lower(), 'doc')


def parse_chat(txt_path: Path, chat_ref: str, line: str) -> list[dict]:
    """Parsea _chat.txt y devuelve lista de mensajes normalizados."""
    msgs: list[dict] = []
    cur: dict | None = None

    with open(txt_path, encoding='utf-8') as f:
        for raw in f:
            raw = raw.rstrip('\n')
            m = LINE_RE.match(raw)

            if m:
                if cur:
                    msgs.append(cur)
                    cur = None

                date_s, time_s, rest = m.group(1), m.group(2), m.group(3)

                # Descartar mensajes del sistema
                if any(p in rest for p in SYSTEM_PHRASES):
                    continue

                # Separar sender del contenido
                if ': ' in rest:
                    sender, content = rest.split(': ', 1)
                else:
                    sender, content = None, rest

                # Timestamp
                try:
                    ts = parse_ts(date_s, time_s)
                except ValueError:
                    ts = f"{date_s} {time_s}"

                # Detectar adjunto
                am = ATTACH_RE.search(content)
                media_ref: str | None = None
                msg_type = 'text'
                if am:
                    media_ref = am.group(1).strip()
                    msg_type = media_type(media_ref)
                    content = ATTACH_RE.sub('', content).strip() or None

                cur = {
                    'line': line,
                    'chat_ref': chat_ref,
                    'sender': sender,
                    'timestamp': ts,
                    'type': msg_type,
                    'content': content,
                    'media_ref': media_ref,
                    'source': 'export',
                    'needs_review': 0,
                }

            else:
                # Continuación de mensaje anterior (multi-línea)
                if cur is not None:
                    cur['content'] = (cur['content'] or '') + '\n' + raw

    if cur:
        msgs.append(cur)
    return msgs


# ── Inserción ────────────────────────────────────────────────────────────────

def upsert(conn: sqlite3.Connection, msgs: list[dict]) -> tuple[int, int]:
    inserted = skipped = 0
    for msg in msgs:
        mid = make_id(
            msg['chat_ref'], msg['sender'] or '',
            msg['timestamp'], msg['content'] or '',
            msg.get('media_ref') or '',
        )
        conn.execute("""
            INSERT OR IGNORE INTO messages
              (message_id, line, chat_ref, sender, timestamp,
               type, content, media_ref, source, needs_review)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (
            mid, msg['line'], msg['chat_ref'], msg['sender'],
            msg['timestamp'], msg['type'], msg['content'],
            msg['media_ref'], msg['source'], msg['needs_review'],
        ))
        if conn.execute("SELECT changes()").fetchone()[0]:
            inserted += 1
        else:
            skipped += 1
    conn.commit()
    return inserted, skipped


# ── Input handling ───────────────────────────────────────────────────────────

def resolve_input(input_path: str) -> tuple[Path, bool]:
    """Devuelve (work_dir, needs_cleanup). Extrae ZIP si es necesario."""
    p = Path(input_path)
    if p.is_dir():
        return p, False
    if p.suffix.lower() == '.zip':
        tmp = Path(tempfile.mkdtemp())
        with zipfile.ZipFile(p) as zf:
            zf.extractall(tmp)
        return tmp, True
    raise ValueError(f"Entrada no soportada: {p.suffix}. Usá un .zip o carpeta extraída.")


def chat_ref_from_txt(txt_path: Path) -> str:
    """Extrae chat_ref del nombre del .txt: 'Chat de WhatsApp con X.txt' → 'group:X'"""
    name = txt_path.stem  # sin extensión
    for prefix in ('Chat de WhatsApp con ', 'Chat de Whatsapp con ', 'WhatsApp Chat with '):
        if name.startswith(prefix):
            return f"group:{name[len(prefix):]}"
    return f"group:{name}"


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(
        description='Parsea export de WhatsApp (.zip o carpeta) a SQLite'
    )
    ap.add_argument('input', help='Ruta al .zip o carpeta del export')
    ap.add_argument('--line', default='maurisito',
                    help='Identificador de línea (default: maurisito)')
    ap.add_argument('--db', default=str(DB_PATH),
                    help=f'Ruta a SQLite DB (default: {DB_PATH})')
    ap.add_argument('--dry-run', action='store_true',
                    help='Parsear sin insertar en DB (verificación)')
    args = ap.parse_args()

    db_path = Path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    work_dir, cleanup = resolve_input(args.input)
    try:
        txt_files = sorted(work_dir.rglob('*.txt'))
        if not txt_files:
            print("ERROR: No encontre ningun .txt en el export.")
            return

        chat_file = txt_files[0]
        chat_ref = chat_ref_from_txt(chat_file)

        print(f"\n[INPUT]  Archivo : {chat_file.name}")
        print(f"         Chat ref: {chat_ref}")
        print(f"         Linea   : {args.line}")

        msgs = parse_chat(chat_file, chat_ref, args.line)

        # Resumen de tipos
        types: dict[str, int] = {}
        for msg in msgs:
            types[msg['type']] = types.get(msg['type'], 0) + 1

        print(f"         Mensajes : {len(msgs)} encontrados")
        for t, n in sorted(types.items()):
            print(f"           {t}: {n}")

        if args.dry_run:
            print("\n[DRY-RUN] Nada insertado en DB.")
            _preview(msgs)
            return

        conn = sqlite3.connect(db_path)
        init_db(conn)
        inserted, skipped = upsert(conn, msgs)
        conn.close()

        print(f"\n    OK  {inserted} insertados · {skipped} duplicados ignorados")
        print(f"    DB: {db_path.resolve()}")

    finally:
        if cleanup:
            shutil.rmtree(work_dir, ignore_errors=True)


def _preview(msgs: list[dict], n: int = 5) -> None:
    print(f"\n-- Primeros {n} mensajes --")
    for msg in msgs[:n]:
        ts = msg['timestamp'][:16]
        sender = (msg['sender'] or 'SISTEMA')[:20]
        content = (msg['content'] or f"[{msg['media_ref']}]")[:60]
        print(f"  {ts}  {sender:<20}  {content}")


if __name__ == '__main__':
    main()
