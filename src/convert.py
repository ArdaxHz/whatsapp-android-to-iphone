"""
Converts Android WhatsApp SQLite DB (msgstore.db) to iOS ChatStorage.sqlite schema.
"""
import sqlite3
import shutil
import time
from pathlib import Path
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

console = Console()

# Apple Core Data epoch starts 2001-01-01; Unix epoch 1970-01-01
# Difference in seconds:
APPLE_EPOCH_OFFSET = 978307200


def unix_to_apple(ts_ms: int) -> float:
    """Convert Android timestamp (milliseconds) to Apple Core Data timestamp (seconds)."""
    if not ts_ms:
        return 0.0
    return (ts_ms / 1000.0) - APPLE_EPOCH_OFFSET


def create_ios_schema(conn: sqlite3.Connection):
    """Creates the iOS WhatsApp ChatStorage.sqlite schema."""
    conn.executescript("""
        PRAGMA journal_mode=WAL;

        CREATE TABLE IF NOT EXISTS Z_PRIMARYKEY (
            Z_ENT   INTEGER PRIMARY KEY,
            Z_NAME  TEXT,
            Z_SUPER INTEGER,
            Z_MAX   INTEGER
        );

        CREATE TABLE IF NOT EXISTS Z_METADATA (
            Z_VERSION  INTEGER PRIMARY KEY,
            Z_UUID     TEXT,
            Z_PLIST    BLOB
        );

        CREATE TABLE IF NOT EXISTS ZWACHATSESSION (
            Z_PK                     INTEGER PRIMARY KEY AUTOINCREMENT,
            Z_ENT                    INTEGER DEFAULT 1,
            Z_OPT                    INTEGER DEFAULT 1,
            ZARCHIVED                INTEGER DEFAULT 0,
            ZCONTACTABID             INTEGER DEFAULT 0,
            ZMESSAGECOUNTER          INTEGER DEFAULT 0,
            ZUNREADCOUNT             INTEGER DEFAULT 0,
            ZLASTMESSAGEDATE         REAL,
            ZCONTACTIDENTIFIER       TEXT,
            ZPARTNERNAME             TEXT,
            ZSESSIONTYPE             INTEGER DEFAULT 0,
            ZGROUPINFO               INTEGER,
            ZLASTMESSAGE             INTEGER
        );

        CREATE TABLE IF NOT EXISTS ZWAGROUPINFO (
            Z_PK            INTEGER PRIMARY KEY AUTOINCREMENT,
            Z_ENT           INTEGER DEFAULT 2,
            Z_OPT           INTEGER DEFAULT 1,
            ZCHATSESSION    INTEGER,
            ZSUBJECT        TEXT,
            ZCREATIONDATE   REAL
        );

        CREATE TABLE IF NOT EXISTS ZWAMESSAGE (
            Z_PK                        INTEGER PRIMARY KEY AUTOINCREMENT,
            Z_ENT                       INTEGER DEFAULT 3,
            Z_OPT                       INTEGER DEFAULT 1,
            ZCHILDMESSAGESDELIVEREDCOUNT INTEGER DEFAULT 0,
            ZCHILDMESSAGESPLAYEDCOUNT    INTEGER DEFAULT 0,
            ZCHILDMESSAGESREADCOUNT      INTEGER DEFAULT 0,
            ZDATAITEMVERSION             INTEGER DEFAULT 0,
            ZDOCID                       INTEGER DEFAULT 0,
            ZENCRETRYCOUNT               INTEGER DEFAULT 0,
            ZFILTEREDRECIPIENTCOUNT      INTEGER DEFAULT 0,
            ZFLAGS                       INTEGER DEFAULT 0,
            ZGROUPEVENTTYPE              INTEGER DEFAULT 0,
            ZISFROMME                    INTEGER DEFAULT 0,
            ZMESSAGEERRORSTATUS          INTEGER DEFAULT 0,
            ZMESSAGESTATUS               INTEGER DEFAULT 0,
            ZMESSAGETYPE                 INTEGER DEFAULT 0,
            ZSORTID                      INTEGER DEFAULT 0,
            ZSPOTLIGHTSTATUS             INTEGER DEFAULT 0,
            ZSTARRED                     INTEGER DEFAULT 0,
            ZCHATSESSION                 INTEGER,
            ZMEDIAITEM                   INTEGER,
            ZPARENTMESSAGE               INTEGER,
            ZMESSAGEDATE                 REAL,
            ZSENTDATE                    REAL,
            ZFROMJID                     TEXT,
            ZGROUPMEMBER                 TEXT,
            ZPUSHNAME                    TEXT,
            ZTEXT                        TEXT,
            ZTOJID                       TEXT,
            ZVCARDNAME                   TEXT,
            ZVCARDSTRING                 TEXT
        );

        CREATE TABLE IF NOT EXISTS ZWAMEDIAITEM (
            Z_PK                INTEGER PRIMARY KEY AUTOINCREMENT,
            Z_ENT               INTEGER DEFAULT 4,
            Z_OPT               INTEGER DEFAULT 1,
            ZFILESIZE           INTEGER DEFAULT 0,
            ZMESSAGE            INTEGER,
            ZCREATIONDATE       REAL,
            ZMEDIASECTION       TEXT,
            ZMEDIAURL           TEXT,
            ZMEDIALOCALPATH     TEXT,
            ZMIMETYPE           TEXT,
            ZTITLE              TEXT,
            ZVCARDSTRING        TEXT,
            ZXMPPTHUMBPATH      TEXT
        );

        CREATE TABLE IF NOT EXISTS ZWAPROFILEPUSHNAME (
            Z_PK        INTEGER PRIMARY KEY AUTOINCREMENT,
            Z_ENT       INTEGER DEFAULT 5,
            Z_OPT       INTEGER DEFAULT 1,
            ZJID        TEXT,
            ZPUSHNAME   TEXT
        );

        CREATE INDEX IF NOT EXISTS ZWAMESSAGE_ZCHATSESSION_INDEX
            ON ZWAMESSAGE (ZCHATSESSION);
        CREATE INDEX IF NOT EXISTS ZWAMESSAGE_ZMESSAGEDATE_INDEX
            ON ZWAMESSAGE (ZMESSAGEDATE);
        CREATE INDEX IF NOT EXISTS ZWACHATSESSION_ZLASTMESSAGEDATE_INDEX
            ON ZWACHATSESSION (ZLASTMESSAGEDATE);

        INSERT OR IGNORE INTO Z_PRIMARYKEY (Z_ENT, Z_NAME, Z_SUPER, Z_MAX)
        VALUES
            (1, 'WAChatSession', 0, 0),
            (2, 'WAGroupInfo',   0, 0),
            (3, 'WAMessage',     0, 0),
            (4, 'WAMediaItem',   0, 0),
            (5, 'WAProfilePushName', 0, 0);
    """)
    conn.commit()


def _get_android_tables(android_conn: sqlite3.Connection) -> list:
    cursor = android_conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    return [r[0] for r in cursor.fetchall()]


def _jid_to_phone(jid: str) -> str:
    """Strip @s.whatsapp.net or @g.us from JID."""
    if not jid:
        return ""
    return jid.split("@")[0]


def convert(android_db: Path, ios_db: Path, media_dir: Path | None = None):
    """
    Main conversion: Android msgstore.db → iOS ChatStorage.sqlite
    """
    console.print("\n[bold]Converting Android database to iOS format...[/bold]")

    if ios_db.exists():
        ios_db.unlink()

    android_conn = sqlite3.connect(str(android_db))
    android_conn.row_factory = sqlite3.Row
    ios_conn = sqlite3.connect(str(ios_db))

    create_ios_schema(ios_conn)

    tables = _get_android_tables(android_conn)
    has_jid_table = "jid" in tables
    has_chat_list = "chat_list" in tables or "chat" in tables

    # --- Build JID lookup ---
    jid_map = {}  # jid_id → jid string (for newer Android schema)
    if has_jid_table:
        for row in android_conn.execute("SELECT _id, raw_string FROM jid"):
            jid_map[row["_id"]] = row["raw_string"]

    # --- Migrate chats ---
    console.print("  → Migrating chats...")
    chat_session_map = {}  # android chat _id → iOS ZWACHATSESSION Z_PK

    chat_query = "SELECT * FROM chat_list" if "chat_list" in tables else "SELECT * FROM chat"
    try:
        chats = android_conn.execute(chat_query).fetchall()
    except Exception:
        chats = []

    for chat in chats:
        chat_dict = dict(chat)
        raw_jid = chat_dict.get("key_remote_jid") or chat_dict.get("jid_row_id", "")
        if isinstance(raw_jid, int):
            raw_jid = jid_map.get(raw_jid, str(raw_jid))

        is_group = "@g.us" in str(raw_jid)
        partner = _jid_to_phone(raw_jid)
        last_ts = unix_to_apple(chat_dict.get("last_message_table_id", 0) or 0)

        ios_conn.execute(
            """INSERT INTO ZWACHATSESSION
               (Z_ENT, ZCONTACTIDENTIFIER, ZPARTNERNAME, ZSESSIONTYPE,
                ZLASTMESSAGEDATE, ZMESSAGECOUNTER, ZUNREADCOUNT)
               VALUES (1, ?, ?, ?, ?, ?, ?)""",
            (
                raw_jid,
                partner,
                1 if is_group else 0,
                last_ts,
                chat_dict.get("message_count", 0),
                chat_dict.get("unseen_message_count", 0),
            ),
        )
        chat_session_map[chat_dict["_id"]] = ios_conn.lastrowid

        if is_group:
            group_subject = chat_dict.get("subject", partner)
            ios_conn.execute(
                "INSERT INTO ZWAGROUPINFO (ZCHATSESSION, ZSUBJECT) VALUES (?, ?)",
                (ios_conn.lastrowid, group_subject),
            )

    ios_conn.commit()
    console.print(f"  [green]✓[/green] {len(chats)} chats migrated")

    # --- Migrate messages ---
    console.print("  → Migrating messages (may take a minute for large histories)...")

    msg_count = android_conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    console.print(f"  Total messages to migrate: [bold]{msg_count:,}[/bold]")

    # Build chat session lookup by JID for messages that reference JID directly
    session_by_jid = {}
    for row in ios_conn.execute("SELECT Z_PK, ZCONTACTIDENTIFIER FROM ZWACHATSESSION"):
        session_by_jid[row[1]] = row[0]

    batch_size = 500
    offset = 0
    migrated = 0
    media_count = 0

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        transient=True,
    ) as progress:
        task = progress.add_task("  Migrating messages...", total=msg_count)

        while True:
            rows = android_conn.execute(
                f"SELECT * FROM messages LIMIT {batch_size} OFFSET {offset}"
            ).fetchall()
            if not rows:
                break

            for msg in rows:
                m = dict(msg)

                # Resolve JID
                jid_raw = m.get("key_remote_jid", "")
                if not jid_raw and "key_remote_jid" not in m:
                    jid_id = m.get("chat_row_id") or m.get("jid_row_id", 0)
                    jid_raw = jid_map.get(jid_id, "")

                # Find or create session
                session_pk = session_by_jid.get(jid_raw)
                if not session_pk:
                    # Create a session for this JID
                    ios_conn.execute(
                        """INSERT INTO ZWACHATSESSION
                           (Z_ENT, ZCONTACTIDENTIFIER, ZPARTNERNAME, ZSESSIONTYPE)
                           VALUES (1, ?, ?, ?)""",
                        (jid_raw, _jid_to_phone(jid_raw), 1 if "@g.us" in jid_raw else 0),
                    )
                    session_pk = ios_conn.lastrowid
                    session_by_jid[jid_raw] = session_pk

                ts = unix_to_apple(m.get("timestamp", 0) or 0)
                is_from_me = int(m.get("key_from_me", 0) or 0)

                # Determine message type
                wa_type = m.get("media_wa_type", 0) or 0
                # 0=text, 1=image, 2=audio, 3=video, 4=vcard, 5=location
                # Map Android type → iOS type (similar mapping)
                ios_type = wa_type

                # Insert message
                ios_conn.execute(
                    """INSERT INTO ZWAMESSAGE
                       (Z_ENT, ZISFROMME, ZMESSAGETYPE, ZMESSAGEDATE, ZSENTDATE,
                        ZTEXT, ZCHATSESSION, ZFROMJID, ZTOJID, ZSTARRED, ZMESSAGESTATUS)
                       VALUES (3, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        is_from_me,
                        ios_type,
                        ts,
                        ts,
                        m.get("data") or "",
                        session_pk,
                        "" if is_from_me else jid_raw,
                        jid_raw if is_from_me else "",
                        int(m.get("starred", 0) or 0),
                        m.get("status", 0) or 0,
                    ),
                )
                msg_pk = ios_conn.lastrowid

                # Media item
                if wa_type in (1, 2, 3, 6, 7, 8, 13, 14, 15, 16) and m.get("media_url"):
                    ios_conn.execute(
                        """INSERT INTO ZWAMEDIAITEM
                           (Z_ENT, ZMESSAGE, ZMEDIAURL, ZMIMETYPE, ZFILESIZE, ZCREATIONDATE)
                           VALUES (4, ?, ?, ?, ?, ?)""",
                        (
                            msg_pk,
                            m.get("media_url", ""),
                            m.get("media_mime_type", ""),
                            m.get("media_size", 0) or 0,
                            ts,
                        ),
                    )
                    media_count += 1

                migrated += 1

            ios_conn.commit()
            offset += batch_size
            progress.update(task, advance=len(rows))

    # Update chat last message dates
    ios_conn.executescript("""
        UPDATE ZWACHATSESSION
        SET ZLASTMESSAGEDATE = (
            SELECT MAX(ZMESSAGEDATE)
            FROM ZWAMESSAGE
            WHERE ZWAMESSAGE.ZCHATSESSION = ZWACHATSESSION.Z_PK
        )
        WHERE EXISTS (
            SELECT 1 FROM ZWAMESSAGE
            WHERE ZWAMESSAGE.ZCHATSESSION = ZWACHATSESSION.Z_PK
        );
    """)
    ios_conn.commit()

    android_conn.close()
    ios_conn.close()

    console.print(f"  [green]✓[/green] {migrated:,} messages converted")
    console.print(f"  [green]✓[/green] {media_count:,} media references converted")
    console.print(f"[green]Database conversion complete →[/green] {ios_db.name}")
    return ios_db
