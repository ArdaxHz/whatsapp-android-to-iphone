"""End-to-end check on synthetic data: python3 test_migration.py"""
import plistlib
import sqlite3
import subprocess
import tempfile
from pathlib import Path

from src.convert import merge
from src.iphone import (CHATSTORAGE, WA_DOMAIN, blob_path, check_backup, extract_chatstorage,
                        file_id, save_originals, write_back)

ME, BOB, GROUP, ALICE = "", "447700900001@s.whatsapp.net", "120363000@g.us", "447700900002@s.whatsapp.net"


def make_android(path: Path):
    a = sqlite3.connect(path)
    a.executescript("""
        CREATE TABLE jid (_id INTEGER PRIMARY KEY, raw_string TEXT);
        CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INT, subject TEXT, archived INT, created_timestamp INT);
        CREATE TABLE message (_id INTEGER PRIMARY KEY, chat_row_id INT, from_me INT, key_id TEXT,
            sender_jid_row_id INT, timestamp INT, message_type INT, text_data TEXT, starred INT);
        CREATE TABLE message_media (message_row_id INT, file_path TEXT);
        INSERT INTO jid VALUES (1,'447700900001@s.whatsapp.net'),(2,'120363000@g.us'),(3,'447700900002@s.whatsapp.net'),(4,'status@broadcast');
        INSERT INTO chat VALUES (1,1,NULL,0,0),(2,2,'Family',0,1600000000000),(3,4,NULL,0,0);
        INSERT INTO message VALUES
            (1,1,0,'K1',0,1600000001000,0,'hi from bob',0),
            (2,1,1,'K2',0,1600000002000,0,'hi back',1),
            (3,1,0,'K3',0,1600000003000,1,'look',0),
            (4,1,0,'K4',0,1600000004000,7,NULL,0),
            (5,2,0,'K5',3,1600000005000,0,'group hello',0),
            (6,3,0,'K6',1,1600000006000,0,'status update',0);
        INSERT INTO message_media VALUES (3,'Media/WhatsApp Images/IMG-1.jpg');
    """)
    a.commit()
    a.close()


def make_ios(path: Path):
    i = sqlite3.connect(path)
    i.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE Z_PRIMARYKEY (Z_ENT INTEGER PRIMARY KEY, Z_NAME TEXT, Z_SUPER INT, Z_MAX INT);
        INSERT INTO Z_PRIMARYKEY VALUES (1,'WAChatSession',0,1),(2,'WAMessage',0,1),(3,'WAGroupInfo',0,0),(4,'WAGroupMember',0,0);
        CREATE TABLE ZWACHATSESSION (Z_PK INTEGER PRIMARY KEY, Z_ENT INT, Z_OPT INT, ZCONTACTJID TEXT, ZPARTNERNAME TEXT,
            ZSESSIONTYPE INT, ZARCHIVED INT, ZMESSAGECOUNTER INT, ZUNREADCOUNT INT, ZGROUPINFO INT, ZLASTMESSAGE INT,
            ZLASTMESSAGETEXT TEXT, ZLASTMESSAGEDATE REAL);
        CREATE TABLE ZWAMESSAGE (Z_PK INTEGER PRIMARY KEY, Z_ENT INT, Z_OPT INT, ZCHATSESSION INT, ZGROUPMEMBER INT,
            ZISFROMME INT, ZMESSAGEDATE REAL, ZSENTDATE REAL, ZMESSAGETYPE INT, ZTEXT TEXT, ZSTANZAID TEXT,
            ZSTARRED INT, ZDATAITEMVERSION INT, ZMESSAGESTATUS INT, ZFROMJID TEXT, ZTOJID TEXT, ZSORT INT);
        CREATE TABLE ZWAGROUPINFO (Z_PK INTEGER PRIMARY KEY, Z_ENT INT, Z_OPT INT, ZCHATSESSION INT, ZCREATIONDATE REAL);
        CREATE TABLE ZWAGROUPMEMBER (Z_PK INTEGER PRIMARY KEY, Z_ENT INT, Z_OPT INT, ZCHATSESSION INT, ZMEMBERJID TEXT,
            ZCONTACTNAME TEXT, ZISACTIVE INT, ZISADMIN INT);
    """)
    # Existing iPhone chat with Bob holding one newer message — must survive and sort last.
    i.execute("INSERT INTO ZWACHATSESSION (Z_PK,Z_ENT,Z_OPT,ZCONTACTJID) VALUES (1,1,1,?)", (BOB,))
    i.execute("INSERT INTO ZWAMESSAGE (Z_PK,Z_ENT,Z_OPT,ZCHATSESSION,ZMESSAGEDATE,ZTEXT,ZSTANZAID,ZSORT) "
              "VALUES (1,2,1,1,800000000,'new on iphone','IOS1',0)")
    i.commit()
    return i  # keep open so the data stays in the -wal file, like a live iPhone backup


def make_backup(root: Path, ios_dir: Path) -> Path:
    backup = root / "backup"
    backup.mkdir()
    (backup / "Manifest.plist").write_bytes(plistlib.dumps({"IsEncrypted": False}))
    m = sqlite3.connect(backup / "Manifest.db")
    m.execute("CREATE TABLE Files (fileID TEXT PRIMARY KEY, domain TEXT, relativePath TEXT, flags INT, file BLOB)")
    for suffix in ("", "-wal", "-shm"):
        src = ios_dir / (CHATSTORAGE + suffix)
        fid = file_id(CHATSTORAGE + suffix)
        dest = blob_path(backup, fid)
        dest.parent.mkdir(exist_ok=True)
        dest.write_bytes(src.read_bytes())
        rec = {"$objects": ["$null", {"Size": dest.stat().st_size, "LastModified": 0}], "$top": {}}
        m.execute("INSERT INTO Files VALUES (?,?,?,1,?)",
                  (fid, WA_DOMAIN, CHATSTORAGE + suffix, plistlib.dumps(rec, fmt=plistlib.FMT_BINARY)))
    m.commit()
    m.close()
    return backup


def main():
    with tempfile.TemporaryDirectory() as t:
        t = Path(t)
        android = t / "msgstore.db"
        make_android(android)
        (t / "ios").mkdir()
        live = make_ios(t / "ios" / CHATSTORAGE)
        backup = make_backup(t, t / "ios")
        live.close()
        assert check_backup(backup)

        original_chat = blob_path(backup, file_id(CHATSTORAGE)).read_bytes()
        rollback = save_originals(backup, t / "originals")
        db = extract_chatstorage(backup, t / "work")
        stats = merge(android, db)
        assert stats == {"chats_new": 1, "chats_merged": 1, "messages": 4, "skipped_dupes": 0}, stats
        write_back(backup, db)

        # Backup now holds a standalone DB, empty WAL, and matching manifest sizes.
        restored = blob_path(backup, file_id(CHATSTORAGE))
        assert blob_path(backup, file_id(CHATSTORAGE + "-wal")).stat().st_size == 0
        m = sqlite3.connect(backup / "Manifest.db")
        rec = plistlib.loads(m.execute("SELECT file FROM Files WHERE fileID=?", (file_id(CHATSTORAGE),)).fetchone()[0])
        assert rec["$objects"][1]["Size"] == restored.stat().st_size
        m.close()

        i = sqlite3.connect(restored)
        assert i.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        bob = [r for r in i.execute("SELECT ZTEXT, ZISFROMME, ZSTARRED, ZTOJID FROM ZWAMESSAGE WHERE ZCHATSESSION=1 ORDER BY ZSORT")]
        assert bob == [("hi from bob", 0, 0, None), ("hi back", 1, 1, BOB),
                       ("[Photo: IMG-1.jpg] look", 0, 0, None), ("new on iphone", None, None, None)], bob
        assert i.execute("SELECT ZMESSAGECOUNTER, ZLASTMESSAGETEXT FROM ZWACHATSESSION WHERE Z_PK=1").fetchone() == (4, "new on iphone")
        grp = i.execute("SELECT Z_PK, ZPARTNERNAME, ZSESSIONTYPE, ZGROUPINFO FROM ZWACHATSESSION WHERE ZCONTACTJID=?", (GROUP,)).fetchone()
        assert grp[1:3] == ("Family", 1) and grp[3], grp
        assert i.execute("SELECT m.ZTEXT, g.ZMEMBERJID FROM ZWAMESSAGE m JOIN ZWAGROUPMEMBER g ON g.Z_PK=m.ZGROUPMEMBER").fetchall() == [("group hello", ALICE)]
        assert i.execute("SELECT COUNT(*) FROM ZWACHATSESSION WHERE ZCONTACTJID LIKE '%broadcast'").fetchone()[0] == 0
        # Core Data PK counters must cover every row we added.
        for name, mx in i.execute("SELECT Z_NAME, Z_MAX FROM Z_PRIMARYKEY"):
            table = "Z" + name.upper()
            assert mx >= (i.execute(f"SELECT MAX(Z_PK) FROM {table}").fetchone()[0] or 0), name
        i.close()

        # Re-running imports nothing twice.
        db2 = extract_chatstorage(backup, t / "work2")
        assert merge(android, db2)["messages"] == 0

        # Rollback restores the original bytes.
        subprocess.run(["bash", str(rollback)], check=True, capture_output=True)
        assert blob_path(backup, file_id(CHATSTORAGE)).read_bytes() == original_chat
    print("ok")


if __name__ == "__main__":
    main()
