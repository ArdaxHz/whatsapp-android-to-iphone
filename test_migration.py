"""End-to-end check on synthetic data (plain + encrypted backups, with media): python3 test_migration.py"""
import os
import plistlib
import sqlite3
import struct
import subprocess
import tempfile
from pathlib import Path

from src.convert import merge, phone_key
from src.iphone import (CHATSTORAGE, WA_DOMAIN, Backup, _aes_unwrap, _aes_wrap, _cbc, _pad,
                        extract_chatstorage, file_id, save_originals, write_back)

BOB, GROUP, ALICE = "447700900001@s.whatsapp.net", "120363000@g.us", "447700900002@s.whatsapp.net"
PHOTO = os.urandom(3 * 1024 * 1024 + 7)  # spans several encryption chunks, not block-aligned
PASSWORD = "hunter2"


def make_android(path: Path):
    a = sqlite3.connect(path)
    a.executescript("""
        CREATE TABLE jid (_id INTEGER PRIMARY KEY, raw_string TEXT);
        CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INT, subject TEXT, archived INT, created_timestamp INT);
        CREATE TABLE message (_id INTEGER PRIMARY KEY, chat_row_id INT, from_me INT, key_id TEXT,
            sender_jid_row_id INT, timestamp INT, message_type INT, text_data TEXT, starred INT);
        CREATE TABLE message_media (message_row_id INT, chat_row_id INT, file_path TEXT, mime_type TEXT, media_duration INT);
        CREATE TABLE jid_map (lid_row_id INT, jid_row_id INT);
        CREATE TABLE message_mentions (message_row_id INT, jid_row_id INT, display_name TEXT);
        INSERT INTO jid VALUES (1,'447700900001@s.whatsapp.net'),(2,'120363000@g.us'),(3,'447700900002@s.whatsapp.net'),(4,'status@broadcast'),(5,'447700900009@s.whatsapp.net'),
                               (6,'98765432101234@lid');
        INSERT INTO jid_map VALUES (6,1);
        INSERT INTO message_mentions VALUES (8,6,NULL),(12,3,NULL);
        INSERT INTO chat VALUES (1,1,NULL,0,0),(2,2,'Family',0,1600000000000),(3,4,NULL,0,0),(4,5,NULL,0,0);
        INSERT INTO message VALUES
            (1,1,0,'K1',0,1600000001000,0,'hi from bob',0),
            (2,1,1,'K2',0,1600000002000,0,'hi back',1),
            (3,1,0,'K3',0,1600000003000,1,'look',0),
            (4,1,0,'K4',0,1600000004000,7,NULL,0),
            (5,2,0,'K5',3,1600000005000,0,'group hello',0),
            (6,3,0,'K6',1,1600000006000,0,'status update',0),
            (7,1,0,'K7',0,1600000007000,3,NULL,0),
            (8,1,0,'K8',0,1600000008000,0,'thanks @98765432101234',0),
            (9,1,0,'K9',0,1600000009000,81,NULL,0),
            (10,1,0,'K10',0,1600000010000,99,NULL,0),
            (11,1,0,'K11',0,1600000011000,55,'odd type with text',0),
            (12,2,0,'K12',3,1600000012000,0,'hey @447700900002',0);
        INSERT INTO message_media VALUES (3,1,'Media/WhatsApp Images/IMG-1.jpg','image/jpeg',0),
                                         (7,1,'Media/WhatsApp Video/gone.mp4','video/mp4',9),
                                         (9,1,'Media/WhatsApp Video Notes/PTV-1.mp4','video/mp4',3);
    """)
    a.commit()
    a.close()


def make_ios(path: Path):
    i = sqlite3.connect(path)
    i.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE Z_PRIMARYKEY (Z_ENT INTEGER PRIMARY KEY, Z_NAME TEXT, Z_SUPER INT, Z_MAX INT);
        INSERT INTO Z_PRIMARYKEY VALUES (1,'WAChatSession',0,1),(2,'WAMessage',0,1),(3,'WAGroupInfo',0,0),
                                        (4,'WAGroupMember',0,0),(5,'WAMediaItem',0,0);
        CREATE TABLE ZWACHATSESSION (Z_PK INTEGER PRIMARY KEY, Z_ENT INT, Z_OPT INT, ZCONTACTJID TEXT, ZPARTNERNAME TEXT,
            ZSESSIONTYPE INT, ZARCHIVED INT, ZMESSAGECOUNTER INT, ZUNREADCOUNT INT, ZGROUPINFO INT, ZLASTMESSAGE INT,
            ZLASTMESSAGETEXT TEXT, ZLASTMESSAGEDATE REAL, ZHIDDEN INT, ZREMOVED INT);
        CREATE TABLE ZWAMESSAGE (Z_PK INTEGER PRIMARY KEY, Z_ENT INT, Z_OPT INT, ZCHATSESSION INT, ZGROUPMEMBER INT,
            ZISFROMME INT, ZMESSAGEDATE REAL, ZSENTDATE REAL, ZMESSAGETYPE INT, ZTEXT TEXT, ZSTANZAID TEXT,
            ZSTARRED INT, ZDATAITEMVERSION INT, ZMESSAGESTATUS INT, ZFROMJID TEXT, ZTOJID TEXT, ZSORT INT, ZMEDIAITEM INT);
        CREATE TABLE ZWAGROUPINFO (Z_PK INTEGER PRIMARY KEY, Z_ENT INT, Z_OPT INT, ZCHATSESSION INT, ZCREATIONDATE REAL);
        CREATE TABLE ZWAGROUPMEMBER (Z_PK INTEGER PRIMARY KEY, Z_ENT INT, Z_OPT INT, ZCHATSESSION INT, ZMEMBERJID TEXT,
            ZCONTACTNAME TEXT, ZISACTIVE INT, ZISADMIN INT);
        CREATE TABLE ZWAMEDIAITEM (Z_PK INTEGER PRIMARY KEY, Z_ENT INT, Z_OPT INT, ZMESSAGE INT, ZMEDIALOCALPATH TEXT,
            ZFILESIZE INT, ZTITLE TEXT, ZMOVIEDURATION INT, ZVCARDSTRING TEXT);
    """)
    i.execute("INSERT INTO ZWACHATSESSION (Z_PK,Z_ENT,Z_OPT,ZCONTACTJID) VALUES (1,1,1,?)", (BOB,))
    i.execute("INSERT INTO ZWAMESSAGE (Z_PK,Z_ENT,Z_OPT,ZCHATSESSION,ZMESSAGEDATE,ZTEXT,ZSTANZAID,ZSORT) "
              "VALUES (1,2,1,1,800000000,'new on iphone','IOS1',0)")
    i.commit()
    return i  # keep open so data stays in the -wal file, like a live iPhone backup


def _tlv(tag: bytes, val) -> bytes:
    data = struct.pack(">L", val) if isinstance(val, int) else val
    return tag + struct.pack(">L", len(data)) + data


def make_keybag(password: str):
    import hashlib
    attrs = {b"SALT": os.urandom(20), b"ITER": 10, b"DPSL": os.urandom(20), b"DPIC": 10}
    p = hashlib.pbkdf2_hmac("sha256", password.encode(), attrs[b"DPSL"], attrs[b"DPIC"], 32)
    p = hashlib.pbkdf2_hmac("sha1", p, attrs[b"SALT"], attrs[b"ITER"], 32)
    blob = _tlv(b"VERS", 3) + _tlv(b"TYPE", 1) + _tlv(b"UUID", os.urandom(16)) + _tlv(b"WRAP", 0)
    blob += b"".join(_tlv(k, v) for k, v in attrs.items())
    class_keys = {}
    for cls in (1, 2, 3, 4):
        class_keys[cls] = os.urandom(32)
        blob += _tlv(b"UUID", os.urandom(16)) + _tlv(b"CLAS", cls) + _tlv(b"WRAP", 2) + _tlv(b"KTYP", 0)
        blob += _tlv(b"WPKY", _aes_wrap(p, class_keys[cls]))
    return blob, class_keys


def make_backup(root: Path, ios_dir: Path, password: str | None) -> Path:
    backup = root / "backup"
    backup.mkdir()
    keys = None
    plist = {"IsEncrypted": bool(password)}
    if password:
        plist["BackupKeyBag"], keys = make_keybag(password)
        manifest_key = os.urandom(32)
        plist["ManifestKey"] = struct.pack("<L", 4) + _aes_wrap(keys[4], manifest_key)
    (backup / "Manifest.plist").write_bytes(plistlib.dumps(plist))

    mpath = root / "manifest_plain.db"
    m = sqlite3.connect(mpath)
    m.execute("CREATE TABLE Files (fileID TEXT PRIMARY KEY, domain TEXT, relativePath TEXT, flags INT, file BLOB)")
    dir_rec = {"$top": {"root": plistlib.UID(1)}, "$objects": ["$null", {"Mode": 0o40755, "Size": 0, "RelativePath": plistlib.UID(2)}, "Message"]}
    m.execute("INSERT INTO Files VALUES (?,?,?,2,?)", (file_id("Message"), WA_DOMAIN, "Message", plistlib.dumps(dir_rec, fmt=plistlib.FMT_BINARY)))
    m.execute("INSERT INTO Files VALUES ('other','HomeDomain','Library',2,?)", (plistlib.dumps(dir_rec, fmt=plistlib.FMT_BINARY),))
    for suffix in ("", "-wal", "-shm"):
        data = (ios_dir / (CHATSTORAGE + suffix)).read_bytes()
        fid = file_id(CHATSTORAGE + suffix)
        dest = backup / fid[:2] / fid
        dest.parent.mkdir(exist_ok=True)
        obj = {"Size": len(data), "LastModified": 0, "ProtectionClass": 3, "RelativePath": plistlib.UID(2)}
        objs = ["$null", obj, CHATSTORAGE + suffix]
        if keys:
            fkey = os.urandom(32)
            objs.append({"NS.data": struct.pack("<L", 3) + _aes_wrap(keys[3], fkey), "$class": plistlib.UID(4)})
            objs.append({"$classname": "NSMutableData", "$classes": ["NSMutableData", "NSData", "NSObject"]})
            obj["EncryptionKey"] = plistlib.UID(3)
            data = _cbc(fkey).encrypt(_pad(data))
        dest.write_bytes(data)
        rec = {"$top": {"root": plistlib.UID(1)}, "$objects": objs}
        m.execute("INSERT INTO Files VALUES (?,?,?,1,?)", (fid, WA_DOMAIN, CHATSTORAGE + suffix, plistlib.dumps(rec, fmt=plistlib.FMT_BINARY)))
    m.commit()
    m.close()
    raw = mpath.read_bytes()
    (backup / "Manifest.db").write_bytes(_cbc(manifest_key).encrypt(_pad(raw)) if password else raw)
    return backup


def run(t: Path, password: str | None):
    android = t / "msgstore.db"
    make_android(android)
    media_root = t / "media"
    (media_root / "WhatsApp Images").mkdir(parents=True)
    (media_root / "WhatsApp Images" / "IMG-1.jpg").write_bytes(PHOTO)
    (media_root / "WhatsApp Video Notes").mkdir()
    (media_root / "WhatsApp Video Notes" / "PTV-1.mp4").write_bytes(b"ptv")
    (t / "ios").mkdir()
    live = make_ios(t / "ios" / CHATSTORAGE)
    path = make_backup(t, t / "ios", password)
    live.close()

    original_manifest = (path / "Manifest.db").read_bytes()
    safe = t / "originals"
    rollback = save_originals(path, safe)
    backup = Backup(path, t / "work_manifest", password)
    assert backup.encrypted == bool(password)
    db = extract_chatstorage(backup, t / "work")
    stats = merge(android, db, media_root, {phone_key(BOB): "Bob Smith"})
    media = stats.pop("media")
    assert stats == {"chats_new": 1, "chats_merged": 1, "messages": 9, "skipped_dupes": 0, "refreshed": 0}, stats
    assert len(media) == 2 and all(m[1].startswith(f"Message/Media/{BOB}/") for m in media), media
    write_back(backup, db, media, safe)

    # Re-open from disk like Finder would, and read everything back.
    check = Backup(path, t / "check_manifest", password)
    assert not check.has(CHATSTORAGE + "-wal") and check.has(CHATSTORAGE)
    out = t / "check"
    out.mkdir()
    check.extract(CHATSTORAGE, out / "c.sqlite")
    check.extract(media[0][1], out / "photo")
    assert (out / "photo").read_bytes() == PHOTO
    assert check.has(media[0][1].rsplit("/", 1)[0]) and check.has(f"Message/Media/{BOB}")  # folder records
    rec = check.record(media[0][1])
    assert rec["$objects"][rec["$top"]["root"].data]["Size"] == len(PHOTO)

    i = sqlite3.connect(out / "c.sqlite")
    assert i.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    bob = i.execute("SELECT ZTEXT, ZISFROMME, ZMESSAGETYPE, ZTOJID FROM ZWAMESSAGE WHERE ZCHATSESSION=1 ORDER BY ZSORT").fetchall()
    assert bob == [("hi from bob", 0, 0, None), ("hi back", 1, 0, BOB), (None, 0, 1, None),
                   ("[Video: gone.mp4]", 0, 0, None), ("thanks @Bob Smith", 0, 0, None), (None, 0, 2, None),
                   ("odd type with text", 0, 0, None), ("new on iphone", 0, 0, None)], bob
    # No address-book name for Alice: shown as a phone number rather than a bare id.
    assert i.execute("SELECT ZTEXT FROM ZWAMESSAGE WHERE ZSTANZAID='K12'").fetchone() == ("hey @+447700900002",)
    item = i.execute("SELECT mi.ZMEDIALOCALPATH, mi.ZTITLE, mi.ZFILESIZE, mi.ZVCARDSTRING FROM ZWAMESSAGE m "
                     "JOIN ZWAMEDIAITEM mi ON mi.Z_PK = m.ZMEDIAITEM AND mi.ZMESSAGE = m.Z_PK WHERE m.ZSTANZAID='K3'").fetchone()
    assert item == (media[0][1][len("Message/"):], "look", len(PHOTO), "image/jpeg"), item
    grp = i.execute("SELECT ZPARTNERNAME, ZSESSIONTYPE, ZGROUPINFO FROM ZWACHATSESSION WHERE ZCONTACTJID=?", (GROUP,)).fetchone()
    assert grp[:2] == ("Family", 1) and grp[2], grp
    # Visible in WhatsApp's chat list, and no empty chat for the contact with no messages.
    assert i.execute("SELECT COUNT(*) FROM ZWACHATSESSION WHERE ZHIDDEN IS NOT 0 OR ZREMOVED IS NOT 0").fetchone()[0] == 0
    assert i.execute("SELECT COUNT(*) FROM ZWACHATSESSION WHERE ZCONTACTJID = '447700900009@s.whatsapp.net'").fetchone()[0] == 0
    assert i.execute("SELECT g.ZMEMBERJID FROM ZWAMESSAGE m JOIN ZWAGROUPMEMBER g ON g.Z_PK=m.ZGROUPMEMBER").fetchall() == [(ALICE,)] * 2
    for name, mx in i.execute("SELECT Z_NAME, Z_MAX FROM Z_PRIMARYKEY").fetchall():
        assert mx >= (i.execute(f"SELECT MAX(Z_PK) FROM Z{name.upper()}").fetchone()[0] or 0), name
    i.close()

    # Re-running imports nothing twice, and brings rows written by older versions up to date.
    old = extract_chatstorage(check, t / "work2")
    with sqlite3.connect(old) as o:
        o.execute("UPDATE ZWAMESSAGE SET ZTEXT='thanks @98765432101234' WHERE ZSTANZAID='K8'")
        o.execute("DELETE FROM ZWAMEDIAITEM WHERE ZMESSAGE=(SELECT Z_PK FROM ZWAMESSAGE WHERE ZSTANZAID='K9')")
        o.execute("UPDATE ZWAMESSAGE SET ZMESSAGETYPE=0, ZMEDIAITEM=NULL, ZTEXT='[Message: PTV-1.mp4]' WHERE ZSTANZAID='K9'")
        o.execute("INSERT INTO ZWAMESSAGE (Z_PK,Z_ENT,Z_OPT,ZCHATSESSION,ZMESSAGETYPE,ZTEXT,ZSTANZAID) VALUES (99,2,1,1,0,'[Message]','K10')")
    again = merge(android, old, media_root, {phone_key(BOB): "Bob Smith"})
    assert again["messages"] == 0 and again["refreshed"] == 3 and len(again["media"]) == 1, again
    with sqlite3.connect(old) as o:
        rows = dict(o.execute("SELECT ZSTANZAID, ZTEXT FROM ZWAMESSAGE WHERE ZSTANZAID IN ('K8','K9','K10')").fetchall())
        assert rows == {"K8": "thanks @Bob Smith", "K9": None}, rows
        assert o.execute("SELECT m.ZMESSAGETYPE FROM ZWAMESSAGE m JOIN ZWAMEDIAITEM mi ON mi.Z_PK=m.ZMEDIAITEM "
                         "WHERE m.ZSTANZAID='K9'").fetchone() == (2,)
    assert merge(android, old, media_root, {phone_key(BOB): "Bob Smith"})["refreshed"] == 0

    # Rollback restores the original manifest and removes added files.
    subprocess.run(["bash", str(rollback)], check=True, capture_output=True)
    assert (path / "Manifest.db").read_bytes() == original_manifest
    added_fid = file_id(media[0][1])
    assert not (path / added_fid[:2] / added_fid).exists()


def main():
    # RFC 3394 §4.1 test vector.
    kek, key = bytes(range(16)), bytes.fromhex("00112233445566778899AABBCCDDEEFF")
    wrapped = bytes.fromhex("1FA68B0A8112B447AEF34BD8FB5A7B829D3E862371D2CFE5")
    assert _aes_wrap(kek, key) == wrapped and _aes_unwrap(kek, wrapped) == key

    for password in (None, PASSWORD):
        with tempfile.TemporaryDirectory() as t:
            run(Path(t), password)
    with tempfile.TemporaryDirectory() as t:
        t = Path(t)
        (t / "ios").mkdir()
        live = make_ios(t / "ios" / CHATSTORAGE)
        path = make_backup(t, t / "ios", PASSWORD)
        live.close()
        try:
            Backup(path, t / "w", "wrong")
            raise AssertionError("wrong password accepted")
        except PermissionError:
            pass
    print("ok")


if __name__ == "__main__":
    main()
