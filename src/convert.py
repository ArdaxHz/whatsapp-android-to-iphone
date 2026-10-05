"""
Merges Android WhatsApp history (msgstore.db) into the iPhone's own ChatStorage.sqlite.

The iOS file is a Core Data store whose schema changes between WhatsApp versions, so we
never create it — we insert rows into the real one taken from the iPhone backup, filling
only columns that exist. Field choices follow watoi (github.com/residentsummer/watoi),
which is known to produce databases WhatsApp accepts. Media messages become text
placeholders ("[Photo: IMG-…jpg] caption"); the files themselves are kept on the Mac.
"""
import sqlite3
from pathlib import Path
from rich.console import Console

console = Console()

APPLE_EPOCH_OFFSET = 978307200  # 2001-01-01 in Unix seconds

SKIP_TYPES = {7, 15}  # system notices, deleted messages
PLACEHOLDERS = {
    1: "Photo", 2: "Audio", 3: "Video", 4: "Contact", 5: "Location", 8: "Call",
    9: "Document", 13: "GIF", 16: "Live location", 20: "Sticker",
    42: "View-once photo", 43: "View-once video",
}


def android_ts_to_apple(ts_ms) -> float | None:
    return ts_ms / 1000.0 - APPLE_EPOCH_OFFSET if ts_ms else None


def message_text(msg_type: int, text: str | None, file_path: str | None) -> str | None:
    """Text shown on iOS. None means: don't import this row."""
    if msg_type in SKIP_TYPES:
        return None
    if msg_type == 0:
        return text
    label = PLACEHOLDERS.get(msg_type, "Message")
    if file_path:
        label += ": " + file_path.rsplit("/", 1)[-1]
    return f"[{label}] {text}" if text else f"[{label}]"


class CoreDataStore:
    """Inserts into a Core Data SQLite store, keeping Z_PRIMARYKEY in sync."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.ents = {name: (ent, mx) for ent, name, mx in conn.execute("SELECT Z_ENT, Z_NAME, Z_MAX FROM Z_PRIMARYKEY")}
        self.cols = {}

    def table(self, entity: str) -> str:
        return "Z" + entity.upper()

    def insert(self, entity: str, values: dict) -> int:
        table = self.table(entity)
        if table not in self.cols:
            self.cols[table] = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
        ent, mx = self.ents[entity]
        pk = mx + 1
        self.ents[entity] = (ent, pk)
        row = {"Z_PK": pk, "Z_ENT": ent, "Z_OPT": 1}
        row.update({k: v for k, v in values.items() if k in self.cols[table]})
        self.conn.execute(
            f"INSERT INTO {table} ({','.join(row)}) VALUES ({','.join('?' * len(row))})",
            list(row.values()),
        )
        return pk

    def save_counters(self):
        for name, (ent, mx) in self.ents.items():
            self.conn.execute("UPDATE Z_PRIMARYKEY SET Z_MAX = ? WHERE Z_ENT = ?", (mx, ent))


def _android_chats(a: sqlite3.Connection):
    tables = {r[0] for r in a.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "message" not in tables or "chat" not in tables or "jid" not in tables:
        raise RuntimeError(
            "This Android database uses an old WhatsApp format. Update WhatsApp on Android, "
            "make a fresh backup, and run again."
        )
    jids = {r[0]: r[1] for r in a.execute("SELECT _id, raw_string FROM jid")}
    # Newer Android versions identify some people by @lid ids; map them back to phone-number JIDs.
    if "jid_map" in tables:
        for lid, pn in a.execute("SELECT lid_row_id, jid_row_id FROM jid_map"):
            if pn in jids:
                jids[lid] = jids[pn]
    chats = [dict(r) for r in a.execute("SELECT * FROM chat")]
    return jids, chats


def merge(android_db: Path, ios_db: Path) -> dict:
    """Merges android_db into ios_db in place. Re-running is safe: already-imported messages are skipped."""
    a = sqlite3.connect(android_db)
    a.row_factory = sqlite3.Row
    i = sqlite3.connect(ios_db)
    store = CoreDataStore(i)
    for need in ("WAChatSession", "WAMessage", "WAGroupInfo", "WAGroupMember"):
        if need not in store.ents:
            raise RuntimeError(f"iPhone ChatStorage.sqlite has no {need} entity — unexpected WhatsApp version.")

    jids, chats = _android_chats(a)
    sessions = {jid: pk for pk, jid in i.execute("SELECT Z_PK, ZCONTACTJID FROM ZWACHATSESSION")}
    stats = {"chats_new": 0, "chats_merged": 0, "messages": 0, "skipped_dupes": 0}

    console.print(f"  → {len(chats)} Android chats")
    for chat in chats:
        chat_jid = jids.get(chat["jid_row_id"])
        if not chat_jid or chat_jid == "status@broadcast" or chat_jid.endswith("@broadcast"):
            continue
        is_group = chat_jid.endswith("@g.us")

        session = sessions.get(chat_jid)
        if session:
            stats["chats_merged"] += 1
        else:
            stats["chats_new"] += 1
            session = store.insert("WAChatSession", {
                "ZCONTACTJID": chat_jid,
                "ZPARTNERNAME": chat.get("subject") if is_group and chat.get("subject") else chat_jid.split("@")[0],
                "ZSESSIONTYPE": 1 if is_group else 0,
                "ZARCHIVED": 1 if chat.get("archived") else 0,
                "ZMESSAGECOUNTER": 0,
                "ZUNREADCOUNT": 0,
            })
            sessions[chat_jid] = session
            if is_group:
                info = store.insert("WAGroupInfo", {
                    "ZCHATSESSION": session,
                    "ZCREATIONDATE": android_ts_to_apple(chat.get("created_timestamp")),
                })
                i.execute("UPDATE ZWACHATSESSION SET ZGROUPINFO = ? WHERE Z_PK = ?", (info, session))

        members = {}
        if is_group:
            members = {jid: pk for pk, jid in i.execute(
                "SELECT Z_PK, ZMEMBERJID FROM ZWAGROUPMEMBER WHERE ZCHATSESSION = ?", (session,))}
        existing_ids = {r[0] for r in i.execute(
            "SELECT ZSTANZAID FROM ZWAMESSAGE WHERE ZCHATSESSION = ? AND ZSTANZAID IS NOT NULL", (session,))}

        rows = a.execute(
            """SELECT m.key_id, m.from_me, m.timestamp, m.message_type, m.text_data, m.starred,
                      m.sender_jid_row_id, mm.file_path
               FROM message m LEFT JOIN message_media mm ON mm.message_row_id = m._id
               WHERE m.chat_row_id = ? ORDER BY m.timestamp, m._id""",
            (chat["_id"],),
        )
        for m in rows:
            text = message_text(m["message_type"], m["text_data"], m["file_path"])
            if text is None or not m["timestamp"]:
                continue
            if m["key_id"] in existing_ids:
                stats["skipped_dupes"] += 1
                continue
            from_me = bool(m["from_me"])
            values = {
                "ZCHATSESSION": session,
                "ZISFROMME": int(from_me),
                "ZMESSAGEDATE": android_ts_to_apple(m["timestamp"]),
                "ZSENTDATE": android_ts_to_apple(m["timestamp"]),
                "ZMESSAGETYPE": 0,
                "ZTEXT": text,
                "ZSTANZAID": m["key_id"],
                "ZSTARRED": int(bool(m["starred"])),
                "ZDATAITEMVERSION": 2,
                "ZMESSAGESTATUS": 5 if from_me else 0,
            }
            if from_me:
                values["ZTOJID"] = chat_jid
            else:
                values["ZFROMJID"] = chat_jid
                sender = jids.get(m["sender_jid_row_id"])
                if is_group and sender:
                    if sender not in members:
                        members[sender] = store.insert("WAGroupMember", {
                            "ZCHATSESSION": session,
                            "ZMEMBERJID": sender,
                            "ZCONTACTNAME": sender.split("@")[0],
                            "ZISACTIVE": 0,
                            "ZISADMIN": 0,
                        })
                    values["ZGROUPMEMBER"] = members[sender]
            store.insert("WAMessage", values)
            existing_ids.add(m["key_id"])
            stats["messages"] += 1

        _resort_chat(i, session)

    store.save_counters()
    i.commit()
    a.close()
    i.close()
    return stats


def _resort_chat(i: sqlite3.Connection, session: int):
    """iOS orders a chat by ZSORT; renumber by date and point the chat at its newest message."""
    pks = [r[0] for r in i.execute(
        "SELECT Z_PK FROM ZWAMESSAGE WHERE ZCHATSESSION = ? ORDER BY ZMESSAGEDATE, Z_PK", (session,))]
    i.executemany("UPDATE ZWAMESSAGE SET ZSORT = ? WHERE Z_PK = ?", list(enumerate(pks)))
    if pks:
        last, text, date = i.execute(
            "SELECT Z_PK, ZTEXT, ZMESSAGEDATE FROM ZWAMESSAGE WHERE Z_PK = ?", (pks[-1],)).fetchone()
        cols = {r[1] for r in i.execute("PRAGMA table_info(ZWACHATSESSION)")}
        updates = {"ZMESSAGECOUNTER": len(pks), "ZLASTMESSAGE": last, "ZLASTMESSAGETEXT": text, "ZLASTMESSAGEDATE": date}
        updates = {k: v for k, v in updates.items() if k in cols}
        i.execute(
            f"UPDATE ZWACHATSESSION SET {', '.join(k + ' = ?' for k in updates)} WHERE Z_PK = ?",
            [*updates.values(), session],
        )
