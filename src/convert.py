"""
Merges Android WhatsApp history (msgstore.db) into the iPhone's own ChatStorage.sqlite.

The iOS file is a Core Data store whose schema changes between WhatsApp versions, so we
never create it — we insert rows into the real one taken from the iPhone backup, filling
only columns that exist. Field choices follow watoi (github.com/residentsummer/watoi),
which is known to produce databases WhatsApp accepts.

Media: when the Android file was pulled, the message becomes a real iOS media message
(WAMediaItem + file at Message/Media/<chat>/<x>/<y>/<uuid>.<ext>) and merge() returns the
files to copy into the backup. Missing files fall back to a "[Photo: …] caption" text.
"""
import mimetypes
from collections import defaultdict
from itertools import groupby
import sqlite3
import uuid
from pathlib import Path
from rich.console import Console

console = Console()

APPLE_EPOCH_OFFSET = 978307200  # 2001-01-01 in Unix seconds

SKIP_TYPES = {7, 15, 64}  # system notices, deleted messages, deleted by admin/sender
PLACEHOLDERS = {
    1: "Photo", 2: "Audio", 3: "Video", 4: "Contact", 5: "Location", 8: "Call",
    9: "Document", 13: "GIF", 14: "Contacts", 16: "Live location", 20: "Sticker",
    42: "View-once photo", 43: "View-once video", 66: "Poll", 81: "Video message",
    82: "View-once voice message", 90: "Call",
}
MIME_LABELS = {"image": "Photo", "video": "Video", "audio": "Audio"}


# Android message_type → iOS ZMESSAGETYPE for media we can carry over as files.
# Video notes (81) and view-once voice (82) go in as plain video/audio.
IOS_MEDIA_TYPES = {1: 1, 3: 2, 13: 2, 81: 2, 2: 3, 82: 3, 9: 8, 20: 15}


def find_media(media_root: Path | None, file_path: str | None) -> Path | None:
    """Android paths look like "Media/WhatsApp Images/IMG-….jpg" (or absolute on old versions)."""
    if not media_root or not file_path or "Media/" not in file_path:
        return None
    local = media_root / file_path.split("Media/", 1)[1]
    return local if local.is_file() else None


def android_ts_to_apple(ts_ms) -> float | None:
    return ts_ms / 1000.0 - APPLE_EPOCH_OFFSET if ts_ms else None


def message_text(msg_type: int, text: str | None, file_path: str | None, mime: str | None = None) -> str | None:
    """Text shown on iOS. None means: don't import this row."""
    if msg_type in SKIP_TYPES:
        return None
    if msg_type == 0:
        return text
    label = PLACEHOLDERS.get(msg_type) or MIME_LABELS.get((mime or "").split("/")[0])
    if not label:
        # Unknown types are mostly system/protocol rows (group changes, key changes, …): keep only real content.
        if not file_path:
            return text or None
        label = "Attachment"
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


def phone_key(number: str) -> str:
    """Last 9 digits: matches "+44 7799 979366", "07799979366" and "447799979366@s.whatsapp.net" alike."""
    digits = "".join(c for c in number.split("@")[0] if c.isdigit())
    return digits[-9:] if len(digits) >= 9 else ""


def _mention_names(a: sqlite3.Connection, jids: dict, contacts: dict) -> dict:
    """message _id → [("@<number>", "@<name>")]. Android writes mentions as raw numbers (or @lid ids)
    in the text; iOS can't resolve them without its own metadata, so we write the name in."""
    tables = {r[0] for r in a.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "message_mentions" not in tables:
        return {}
    profile = {}  # WhatsApp profile names Android saw, keyed by phone JID
    if "lid_display_name" in tables:
        for lid, name in a.execute("SELECT lid_row_id, display_name FROM lid_display_name WHERE display_name != ''"):
            if lid in jids:
                profile[jids[lid]] = name
    users = {r[0]: (r[1] or "").split("@")[0] for r in a.execute("SELECT _id, raw_string FROM jid")}
    groups = {r[0]: r[1] for r in a.execute("SELECT jid_row_id, subject FROM chat WHERE subject != ''")}
    out = defaultdict(list)
    for msg, jid_row, shown in a.execute("SELECT message_row_id, jid_row_id, display_name FROM message_mentions"):
        user, jid = users.get(jid_row), jids.get(jid_row, "")
        if not user:
            continue
        if jid.endswith("@g.us"):  # group/community mention, written as "@<id>@g.us"
            if jid_row in groups:
                out[msg].append(("@" + jid, "@" + groups[jid_row]))
            continue
        number = jid.split("@")[0] if jid.endswith("@s.whatsapp.net") else None
        name = (number and contacts.get(phone_key(number))) or profile.get(jid) or shown or (number and "+" + number)
        if name:
            out[msg].append(("@" + user, "@" + name))
    return out


def merge(android_db: Path, ios_db: Path, media_root: Path | None = None, contacts: dict | None = None) -> dict:
    """Merges android_db into ios_db in place. Re-running is safe: messages already there are not duplicated;
    text rows from an earlier run are refreshed (text, mentions, media). contacts: phone_key() → address-book name.
    Returns stats; stats["media"] lists (local file, backup relativePath) pairs to copy into the backup."""
    a = sqlite3.connect(android_db)
    a.row_factory = sqlite3.Row
    i = sqlite3.connect(ios_db)
    store = CoreDataStore(i)
    for need in ("WAChatSession", "WAMessage", "WAGroupInfo", "WAGroupMember"):
        if need not in store.ents:
            raise RuntimeError(f"iPhone ChatStorage.sqlite has no {need} entity — unexpected WhatsApp version.")

    jids, chats = _android_chats(a)
    mentions = _mention_names(a, jids, contacts or {})
    # Decrypted backups ship without indexes; without this each chat rescans every message (hours).
    a.execute("CREATE INDEX IF NOT EXISTS migrate_message_chat ON message (chat_row_id, timestamp)")
    sessions = {jid: pk for pk, jid in i.execute("SELECT Z_PK, ZCONTACTJID FROM ZWACHATSESSION")}
    # Read the iPhone side once — per-chat queries are quadratic on large histories.
    # stanza id → Z_PK for plain-text rows (refreshable), None for anything else (left alone).
    known_ids = defaultdict(dict)
    for session, sid, pk, plain in i.execute(
            "SELECT ZCHATSESSION, ZSTANZAID, Z_PK, ZMESSAGETYPE = 0 AND ZMEDIAITEM IS NULL "
            "FROM ZWAMESSAGE WHERE ZSTANZAID IS NOT NULL"):
        known_ids[session][sid] = pk if plain else None
    known_members = defaultdict(dict)
    for pk, session, jid in i.execute("SELECT Z_PK, ZCHATSESSION, ZMEMBERJID FROM ZWAGROUPMEMBER"):
        known_members[session][jid] = pk
    touched = set()
    stats = {"chats_new": 0, "chats_merged": 0, "messages": 0, "skipped_dupes": 0, "refreshed": 0, "media": []}
    has_media_item = "WAMediaItem" in store.ents

    def attach_media(msg_pk: int, m, src: Path, chat_jid: str):
        u = str(uuid.uuid4())
        local_path = f"Media/{chat_jid}/{u[0]}/{u[1]}/{u}{src.suffix.lower()}"
        keys = m.keys()
        item = store.insert("WAMediaItem", {
            "ZMESSAGE": msg_pk,
            "ZMEDIALOCALPATH": local_path,
            "ZFILESIZE": src.stat().st_size,
            "ZTITLE": m["text_data"] or (src.name if m["message_type"] == 9 else None),
            "ZMOVIEDURATION": m["media_duration"] if "media_duration" in keys else None,
            "ZVCARDSTRING": (m["mime_type"] if "mime_type" in keys else None) or mimetypes.guess_type(src.name)[0],
        })
        i.execute("UPDATE ZWAMESSAGE SET ZMESSAGETYPE = ?, ZTEXT = NULL, ZMEDIAITEM = ? WHERE Z_PK = ?",
                  (IOS_MEDIA_TYPES[m["message_type"]], item, msg_pk))
        stats["media"].append((src, "Message/" + local_path))

    console.print(f"  → {len(chats)} Android chats")
    for n, chat in enumerate(chats, 1):
        if n % 500 == 0 or n == len(chats):
            console.print(f"    {n:,}/{len(chats):,} chats, {stats['messages']:,} messages so far")
        chat_jid = jids.get(chat["jid_row_id"])
        if not chat_jid or chat_jid == "status@broadcast" or chat_jid.endswith("@broadcast"):
            continue
        is_group = chat_jid.endswith("@g.us")

        rows = []
        for m in a.execute(
            """SELECT m._id AS msg_id, m.key_id, m.from_me, m.timestamp, m.message_type, m.text_data, m.starred,
                      m.sender_jid_row_id, mm.*
               FROM message m LEFT JOIN message_media mm ON mm.message_row_id = m._id
               WHERE m.chat_row_id = ? ORDER BY m.timestamp, m._id""",
            (chat["_id"],),
        ):
            if not m["timestamp"]:
                continue
            body = m["text_data"]
            for raw, named in sorted(mentions.get(m["msg_id"], ()), key=lambda r: -len(r[0])):
                body = body and body.replace(raw, named)
            mime = m["mime_type"] if "mime_type" in m.keys() else None
            rows.append((m, message_text(m["message_type"], body, m["file_path"], mime)))
        session = sessions.get(chat_jid)
        if not session and all(text is None for _, text in rows):
            continue  # contacts with no messages would show up as thousands of empty chats

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

        members = known_members[session]
        existing_ids = known_ids[session]

        for m, text in rows:
            src = find_media(media_root, m["file_path"]) if has_media_item else None
            as_media = bool(src) and m["message_type"] in IOS_MEDIA_TYPES
            if m["key_id"] in existing_ids:
                stats["skipped_dupes"] += 1
                pk = existing_ids[m["key_id"]]
                if pk is None:
                    continue
                # A text row from an earlier run: bring it in line with the current rules.
                if text is None:  # only our own "[Message]" placeholders for rows now skipped
                    changed = i.execute("DELETE FROM ZWAMESSAGE WHERE Z_PK = ? AND ZTEXT LIKE '[Message%'", (pk,)).rowcount
                elif as_media:
                    attach_media(pk, m, src, chat_jid)
                    changed = 1
                else:
                    changed = i.execute("UPDATE ZWAMESSAGE SET ZTEXT = ? WHERE Z_PK = ? AND ZTEXT IS NOT ?",
                                        (text, pk, text)).rowcount
                if changed:
                    existing_ids[m["key_id"]] = None
                    stats["refreshed"] += 1
                    touched.add(session)
                continue
            if text is None:
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
                "ZDATAITEMVERSION": 3,
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
            msg_pk = store.insert("WAMessage", values)
            if as_media:
                attach_media(msg_pk, m, src, chat_jid)
            existing_ids[m["key_id"]] = None
            stats["messages"] += 1
            touched.add(session)

    console.print("  → Ordering messages...")
    _resort_chats(i, touched)
    _repair(i)
    store.save_counters()
    i.commit()
    a.close()
    i.close()
    return stats


# Columns WhatsApp always fills (Core Data defaults). Left NULL, WhatsApp's chat list filters the
# chats out (e.g. "hidden == NO AND removed == NO"). Observed in a real iPhone ChatStorage.sqlite.
ZERO_DEFAULTS = {
    "ZWACHATSESSION": ["ZARCHIVED", "ZCONTACTABID", "ZFLAGS", "ZHIDDEN", "ZREMOVED", "ZIDENTITYVERIFICATIONEPOCH",
                       "ZIDENTITYVERIFICATIONSTATE", "ZSPOTLIGHTSTATUS", "ZUNREADCOUNT", "ZMESSAGECOUNTER"],
    "ZWAMESSAGE": ["ZCHILDMESSAGESDELIVEREDCOUNT", "ZCHILDMESSAGESPLAYEDCOUNT", "ZCHILDMESSAGESREADCOUNT", "ZDOCID",
                   "ZENCRETRYCOUNT", "ZFILTEREDRECIPIENTCOUNT", "ZFLAGS", "ZGROUPEVENTTYPE", "ZMESSAGEERRORSTATUS",
                   "ZSPOTLIGHTSTATUS", "ZSTARRED", "ZISFROMME", "ZMESSAGESTATUS", "ZMESSAGETYPE"],
    "ZWAMEDIAITEM": ["ZMEDIAORIGIN", "ZASPECTRATIO", "ZHACCURACY", "ZLATITUDE", "ZLONGITUDE", "ZFILESIZE"],
    "ZWAGROUPINFO": ["ZSTATE"],
    "ZWAGROUPMEMBER": ["ZISACTIVE", "ZISADMIN"],
}


def _repair(i: sqlite3.Connection):
    """Fills WhatsApp's always-set columns and removes empty imported chats. Also fixes earlier runs' output."""
    empty = [(r[0],) for r in i.execute(
        """SELECT Z_PK FROM ZWACHATSESSION s WHERE ZLASTMESSAGEDATE IS NULL
           AND NOT EXISTS (SELECT 1 FROM ZWAMESSAGE m WHERE m.ZCHATSESSION = s.Z_PK)""")]
    for table in ("ZWAGROUPMEMBER", "ZWAGROUPINFO"):
        i.executemany(f"DELETE FROM {table} WHERE ZCHATSESSION = ?", empty)
    i.executemany("DELETE FROM ZWACHATSESSION WHERE Z_PK = ?", empty)
    for table, cols in ZERO_DEFAULTS.items():
        existing = {r[1] for r in i.execute(f"PRAGMA table_info({table})")}
        for col in cols:
            if col in existing:
                i.execute(f"UPDATE {table} SET {col} = 0 WHERE {col} IS NULL")
    # Match what WhatsApp itself writes on every message (seen on all native rows of a real iPhone DB).
    msg_cols = {r[1] for r in i.execute("PRAGMA table_info(ZWAMESSAGE)")}
    for col, sql in (
        ("ZDATAITEMVERSION", "UPDATE ZWAMESSAGE SET ZDATAITEMVERSION = 3 WHERE ZDATAITEMVERSION IS NULL OR ZDATAITEMVERSION < 3"),
        ("ZFLAGS", "UPDATE ZWAMESSAGE SET ZFLAGS = ZFLAGS | 16777216 WHERE (ZFLAGS & 16777216) = 0"),
        ("ZSPOTLIGHTSTATUS", "UPDATE ZWAMESSAGE SET ZSPOTLIGHTSTATUS = -32768 WHERE ZSPOTLIGHTSTATUS = 0"),
    ):
        if col in msg_cols:
            i.execute(sql)


def _resort_chats(i: sqlite3.Connection, sessions: set):
    """iOS orders a chat by ZSORT; renumber by date and point each chat at its newest message. One table pass."""
    cols = {r[1] for r in i.execute("PRAGMA table_info(ZWACHATSESSION)")}
    rows = i.execute(
        "SELECT ZCHATSESSION, Z_PK FROM ZWAMESSAGE ORDER BY ZCHATSESSION, ZMESSAGEDATE, Z_PK").fetchall()
    for session, group in groupby(rows, key=lambda r: r[0]):
        if session not in sessions:
            continue
        pks = [pk for _, pk in group]
        i.executemany("UPDATE ZWAMESSAGE SET ZSORT = ? WHERE Z_PK = ?", list(enumerate(pks)))
        last, text, date = i.execute(
            "SELECT Z_PK, ZTEXT, ZMESSAGEDATE FROM ZWAMESSAGE WHERE Z_PK = ?", (pks[-1],)).fetchone()
        updates = {"ZMESSAGECOUNTER": len(pks), "ZLASTMESSAGE": last, "ZLASTMESSAGETEXT": text, "ZLASTMESSAGEDATE": date}
        updates = {k: v for k, v in updates.items() if k in cols}
        i.execute(
            f"UPDATE ZWACHATSESSION SET {', '.join(k + ' = ?' for k in updates)} WHERE Z_PK = ?",
            [*updates.values(), session],
        )
