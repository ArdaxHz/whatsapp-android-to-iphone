"""
Reads and edits a local Finder backup of the iPhone — encrypted or not.

Encrypted backups: Manifest.plist holds a keybag whose class keys are unlocked by the
backup password. Every file is AES-256-CBC encrypted with its own key, stored wrapped
(RFC 3394) by a class key in the file's Manifest.db record. Manifest.db itself is
encrypted with ManifestKey. We reuse the same scheme for every file we write.
"""
import copy
import hashlib
import os
import plistlib
import shutil
import sqlite3
import struct
import time
from datetime import datetime
from pathlib import Path

from Cryptodome.Cipher import AES
from rich.console import Console
from rich.prompt import Prompt

console = Console()

WA_DOMAIN = "AppDomainGroup-group.net.whatsapp.WhatsApp.shared"
CHATSTORAGE = "ChatStorage.sqlite"
BACKUP_BASE = Path.home() / "Library" / "Application Support" / "MobileSync" / "Backup"
CHUNK = 1 << 20


# --- crypto helpers -----------------------------------------------------------

def _aes_unwrap(kek: bytes, wrapped: bytes) -> bytes | None:
    n = len(wrapped) // 8 - 1
    a = wrapped[:8]
    r = [wrapped[8 * i:8 * i + 8] for i in range(1, n + 1)]
    ecb = AES.new(kek, AES.MODE_ECB)
    for j in reversed(range(6)):
        for i in reversed(range(n)):
            t = struct.pack(">Q", struct.unpack(">Q", a)[0] ^ (n * j + i + 1))
            b = ecb.decrypt(t + r[i])
            a, r[i] = b[:8], b[8:]
    return b"".join(r) if a == b"\xa6" * 8 else None


def _aes_wrap(kek: bytes, key: bytes) -> bytes:
    n = len(key) // 8
    a = b"\xa6" * 8
    r = [key[8 * i:8 * i + 8] for i in range(n)]
    ecb = AES.new(kek, AES.MODE_ECB)
    for j in range(6):
        for i in range(n):
            b = ecb.encrypt(a + r[i])
            a = struct.pack(">Q", struct.unpack(">Q", b[:8])[0] ^ (n * j + i + 1))
            r[i] = b[8:]
    return a + b"".join(r)


def _cbc(key: bytes):
    return AES.new(key, AES.MODE_CBC, iv=b"\x00" * 16)


def _pad(data: bytes) -> bytes:
    n = 16 - len(data) % 16
    return data + bytes([n]) * n


def _encrypt_file(src: Path, dest: Path, key: bytes):
    cipher = _cbc(key)
    with open(src, "rb") as f, open(dest, "wb") as out:
        while True:
            chunk = f.read(CHUNK)
            if len(chunk) < CHUNK:
                out.write(cipher.encrypt(_pad(chunk)))
                return
            out.write(cipher.encrypt(chunk))


def _decrypt_file(src: Path, dest: Path, key: bytes, size: int):
    cipher = _cbc(key)
    with open(src, "rb") as f, open(dest, "wb") as out:
        while chunk := f.read(CHUNK):
            out.write(cipher.decrypt(chunk))
        out.truncate(size)  # drop padding


class Keybag:
    def __init__(self, blob: bytes):
        self.attrs, self.classes, cur = {}, {}, None
        i = 0
        while i + 8 <= len(blob):
            tag, length = blob[i:i + 4], struct.unpack(">L", blob[i + 4:i + 8])[0]
            data = blob[i + 8:i + 8 + length]
            i += 8 + length
            if len(data) == 4:
                data = struct.unpack(">L", data)[0]
            if tag == b"UUID" and b"UUID" in self.attrs:
                cur = {}
            if cur is None:
                self.attrs.setdefault(tag, data)
            else:
                cur[tag] = data
                if tag == b"CLAS":
                    self.classes[data] = cur

    def unlock(self, password: str) -> bool:
        p = hashlib.pbkdf2_hmac("sha256", password.encode(), self.attrs[b"DPSL"], self.attrs[b"DPIC"], 32)
        p = hashlib.pbkdf2_hmac("sha1", p, self.attrs[b"SALT"], self.attrs[b"ITER"], 32)
        for ck in self.classes.values():
            if b"WPKY" in ck and ck[b"WRAP"] & 2:
                ck[b"KEY"] = _aes_unwrap(p, ck[b"WPKY"])
                if ck[b"KEY"] is None:
                    return False
        return True

    def unwrap(self, wrapped_with_class: bytes) -> bytes:
        cls = struct.unpack("<L", wrapped_with_class[:4])[0]
        return _aes_unwrap(self.classes[cls][b"KEY"], wrapped_with_class[4:])

    def wrap(self, cls: int, key: bytes) -> bytes:
        return struct.pack("<L", cls) + _aes_wrap(self.classes[cls][b"KEY"], key)


# --- backup -------------------------------------------------------------------

def file_id(rel_path: str, domain: str = WA_DOMAIN) -> str:
    return hashlib.sha1(f"{domain}-{rel_path}".encode()).hexdigest()


class Backup:
    """Edits happen on a working copy of Manifest.db; nothing is final until save()."""

    def __init__(self, path: Path, work_dir: Path, password: str | None = None):
        self.path, self.work = path, work_dir
        work_dir.mkdir(parents=True, exist_ok=True)
        self.plist = plistlib.loads((path / "Manifest.plist").read_bytes())
        self.encrypted = bool(self.plist.get("IsEncrypted"))
        self.manifest = work_dir / "Manifest.db"
        self.manifest.unlink(missing_ok=True)
        if self.encrypted:
            self.keybag = Keybag(self.plist["BackupKeyBag"])
            if not password or not self.keybag.unlock(password):
                raise PermissionError("Wrong backup password.")
            self.manifest_key = self.keybag.unwrap(self.plist["ManifestKey"])
            self.manifest.write_bytes(_cbc(self.manifest_key).decrypt((path / "Manifest.db").read_bytes()))
        else:
            shutil.copyfile(path / "Manifest.db", self.manifest)
        self.db = sqlite3.connect(self.manifest)
        self.password = password
        self.added: list[str] = []

    def blob(self, fid: str) -> Path:
        return self.path / fid[:2] / fid

    def record(self, rel: str) -> dict | None:
        row = self.db.execute("SELECT file FROM Files WHERE fileID = ?", (file_id(rel),)).fetchone()
        return plistlib.loads(row[0]) if row else None

    def has(self, rel: str) -> bool:
        return self.record(rel) is not None

    def extract(self, rel: str, dest: Path):
        rec = self.record(rel)
        obj = rec["$objects"][rec["$top"]["root"].data]
        src = self.blob(file_id(rel))
        if self.encrypted and "EncryptionKey" in obj:
            key = self.keybag.unwrap(rec["$objects"][obj["EncryptionKey"].data]["NS.data"])
            _decrypt_file(src, dest, key, obj["Size"])
        else:
            shutil.copyfile(src, dest)

    def delete(self, rel: str):
        self.db.execute("DELETE FROM Files WHERE fileID = ?", (file_id(rel),))

    def _new_record(self, template: dict, rel: str, size: int, key: bytes | None) -> bytes:
        rec = copy.deepcopy(template)
        objs = rec["$objects"]
        obj = objs[rec["$top"]["root"].data]
        objs.append(rel)
        obj["RelativePath"] = plistlib.UID(len(objs) - 1)
        obj["Size"] = size
        now = int(time.time())
        obj["LastModified"] = obj["LastStatusChange"] = obj["Birth"] = now
        obj.pop("ExtendedAttributes", None)
        if key is not None:
            enc = dict(objs[obj["EncryptionKey"].data])
            enc["NS.data"] = self.keybag.wrap(obj["ProtectionClass"], key)
            objs.append(enc)
            obj["EncryptionKey"] = plistlib.UID(len(objs) - 1)
        return plistlib.dumps(rec, fmt=plistlib.FMT_BINARY)

    def put_file(self, rel: str, src: Path, template_rel: str = CHATSTORAGE, root_dir: Path | None = None, db=None):
        """Adds or replaces a file, modelled on an existing file record (optionally into another backup folder)."""
        fid = file_id(rel)
        dest = (root_dir or self.path) / fid[:2] / fid
        dest.parent.mkdir(exist_ok=True)
        template = self.record(template_rel)
        root = template["$objects"][template["$top"]["root"].data]
        key = os.urandom(32) if self.encrypted and "EncryptionKey" in root else None
        if root_dir is None and not self.has(rel):
            self.added.append(fid)
        if key:
            _encrypt_file(src, dest, key)
        else:
            shutil.copyfile(src, dest)
        (db or self.db).execute(
            "INSERT OR REPLACE INTO Files (fileID, domain, relativePath, flags, file) VALUES (?, ?, ?, 1, ?)",
            (fid, WA_DOMAIN, rel, self._new_record(template, rel, src.stat().st_size, key)),
        )

    def ensure_dir(self, rel: str, template_rel: str):
        if self.has(rel):
            return
        parent = rel.rsplit("/", 1)[0] if "/" in rel else None
        if parent:
            self.ensure_dir(parent, template_rel)
        self.db.execute(
            "INSERT INTO Files (fileID, domain, relativePath, flags, file) VALUES (?, ?, ?, 2, ?)",
            (file_id(rel), WA_DOMAIN, rel, self._new_record(self.record(template_rel), rel, 0, None)),
        )

    def dir_template(self) -> str:
        row = self.db.execute(
            "SELECT relativePath FROM Files WHERE domain = ? AND flags = 2 AND relativePath != '' "
            "ORDER BY length(relativePath) LIMIT 1", (WA_DOMAIN,)).fetchone()
        if not row:
            raise RuntimeError("No WhatsApp folders found in the backup.")
        return row[0]

    def _write_manifest(self, plain: Path, dest: Path):
        if self.encrypted:
            dest.write_bytes(_cbc(self.manifest_key).encrypt(_pad(plain.read_bytes())))
        else:
            shutil.copyfile(plain, dest)

    def save(self):
        self.db.commit()
        self.db.close()
        self._write_manifest(self.manifest, self.path / "Manifest.db")
        self.db = sqlite3.connect(self.manifest)  # still readable for export_whatsapp_only

    def export_whatsapp_only(self, dest_root: Path) -> Path:
        """Builds a partial backup holding only WhatsApp's shared container (chats + media).

        Restored with RemoveItemsNotRestored off, the phone overwrites just these files and
        keeps every other app, setting and login. Media blobs are hard-linked, not copied.
        """
        dest = dest_root / self.path.name
        shutil.rmtree(dest, ignore_errors=True)
        dest.mkdir(parents=True)
        for name in ("Info.plist", "Status.plist", "Manifest.plist"):
            if (self.path / name).exists():
                shutil.copy2(self.path / name, dest / name)

        plain = self.work / "Manifest-whatsapp-only.db"
        plain.unlink(missing_ok=True)
        slim = sqlite3.connect(plain)
        for (sql,) in self.db.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"):
            slim.execute(sql)
        if self.db.execute("SELECT 1 FROM sqlite_master WHERE name = 'Properties'").fetchone():
            slim.executemany("INSERT INTO Properties VALUES (?, ?)", self.db.execute("SELECT * FROM Properties"))
        rows = self.db.execute("SELECT * FROM Files WHERE domain = ?", (WA_DOMAIN,)).fetchall()
        slim.executemany(f"INSERT INTO Files VALUES ({','.join('?' * len(rows[0]))})", rows)
        for fid, flags in self.db.execute("SELECT fileID, flags FROM Files WHERE domain = ?", (WA_DOMAIN,)):
            src = self.blob(fid)
            if flags != 1 or not src.exists():
                continue
            (dest / fid[:2]).mkdir(exist_ok=True)
            try:
                os.link(src, dest / fid[:2] / fid)
            except OSError:
                shutil.copyfile(src, dest / fid[:2] / fid)
        # The phone's live WAL would otherwise be replayed over the new database.
        empty = self.work / "empty"
        empty.write_bytes(b"")
        for suffix in ("-wal", "-shm"):
            self.put_file(CHATSTORAGE + suffix, empty, root_dir=dest, db=slim)
        slim.commit()
        slim.close()
        self._write_manifest(plain, dest / "Manifest.db")
        return dest_root


# --- user flow ----------------------------------------------------------------

def choose_backup() -> Path | None:
    try:
        backups = [d for d in BACKUP_BASE.iterdir() if (d / "Manifest.db").exists()]
    except PermissionError:
        console.print(
            "[red]macOS blocked access to the backup folder.[/red]\n"
            "System Settings → Privacy & Security → Full Disk Access → enable your Terminal app, then run again."
        )
        return None
    except FileNotFoundError:
        backups = []
    if not backups:
        console.print("[red]No local iPhone backups found.[/red]")
        return None

    backups.sort(key=lambda d: (d / "Manifest.db").stat().st_mtime, reverse=True)
    for n, d in enumerate(backups, 1):
        info = {}
        try:
            info = plistlib.loads((d / "Info.plist").read_bytes())
        except Exception:
            pass
        when = datetime.fromtimestamp((d / "Manifest.db").stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        console.print(f"  {n}. {info.get('Device Name', '?')} ({info.get('Product Type', '?')}) — {when}  [dim]{d.name}[/dim]")
    pick = Prompt.ask("Which backup is your iPhone's NEW backup?",
                      choices=[str(n) for n in range(1, len(backups) + 1)], default="1")
    return backups[int(pick) - 1]


def open_backup(path: Path, work_dir: Path) -> Backup | None:
    encrypted = plistlib.loads((path / "Manifest.plist").read_bytes()).get("IsEncrypted")
    for _ in range(3 if encrypted else 1):
        password = Prompt.ask("iPhone backup password (the one set in Finder)", password=True) if encrypted else None
        try:
            if encrypted:
                console.print("  [dim]Unlocking backup (takes ~10 seconds)...[/dim]")
            backup = Backup(path, work_dir, password)
            break
        except PermissionError:
            console.print("[yellow]Wrong password, try again.[/yellow]")
    else:
        return None
    if not backup.has(CHATSTORAGE):
        console.print(
            "[red]WhatsApp's chat database is not in this backup.[/red]\n"
            "Install WhatsApp on the iPhone, verify your number, open it once, then back up again."
        )
        return None
    return backup


def save_originals(backup_path: Path, safe_dir: Path) -> Path:
    """Copies the files we will overwrite and writes rollback.sh, which also removes files we add."""
    safe_dir.mkdir(parents=True, exist_ok=True)
    lines = ["#!/bin/bash", "# Puts the iPhone backup back exactly as it was before migration.", "set -e"]
    for name in ("Manifest.db",):
        shutil.copy2(backup_path / name, safe_dir / name)
        lines.append(f'cp "{safe_dir / name}" "{backup_path / name}"')
    for suffix in ("", "-wal", "-shm"):
        fid = file_id(CHATSTORAGE + suffix)
        src = backup_path / fid[:2] / fid
        if src.exists():
            shutil.copy2(src, safe_dir / fid)
            lines.append(f'cp "{safe_dir / fid}" "{src}"')
    lines.append(f'while read -r f; do rm -f "{backup_path}/${{f:0:2}}/$f"; done < "{safe_dir / "added_files.txt"}" 2>/dev/null || true')
    rollback = safe_dir / "rollback.sh"
    rollback.write_text("\n".join(lines) + "\necho 'Backup restored to original.'\n")
    rollback.chmod(0o755)
    return rollback


def extract_chatstorage(backup: Backup, work_dir: Path) -> Path:
    """Pulls ChatStorage (+ its WAL) out and folds the WAL in, giving one self-contained file."""
    work_dir.mkdir(parents=True, exist_ok=True)
    db = work_dir / CHATSTORAGE
    for suffix in ("", "-wal", "-shm"):
        (work_dir / (CHATSTORAGE + suffix)).unlink(missing_ok=True)
    for suffix in ("", "-wal"):
        if backup.has(CHATSTORAGE + suffix):
            backup.extract(CHATSTORAGE + suffix, work_dir / (CHATSTORAGE + suffix))
    conn = sqlite3.connect(db)
    if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        conn.close()
        raise RuntimeError("The iPhone's WhatsApp database in this backup is damaged. Make a fresh backup.")
    conn.execute("PRAGMA journal_mode=DELETE")  # merges -wal into the main file
    conn.close()
    return db


def write_back(backup: Backup, new_db: Path, media: list, safe_dir: Path):
    """Writes the merged database and media files into the backup, then saves Manifest.db."""
    need = sum(src.stat().st_size for src, _ in media) + new_db.stat().st_size
    free = shutil.disk_usage(backup.path).free
    if need * 1.1 > free:
        raise RuntimeError(f"Not enough disk space: need {need / 1e9:.1f} GB, have {free / 1e9:.1f} GB.")

    backup.put_file(CHATSTORAGE, new_db)
    # A stale WAL replayed over the new database would corrupt it.
    backup.delete(CHATSTORAGE + "-wal")
    backup.delete(CHATSTORAGE + "-shm")

    if media:
        dir_tpl = backup.dir_template()
        console.print(f"  → Copying {len(media):,} media files into the backup...")
        for n, (src, rel) in enumerate(media, 1):
            backup.ensure_dir(rel.rsplit("/", 1)[0], dir_tpl)
            backup.put_file(rel, src)
            if n % 500 == 0:
                console.print(f"    {n:,}/{len(media):,}")
    safe_dir.mkdir(parents=True, exist_ok=True)
    (safe_dir / "added_files.txt").write_text("\n".join(backup.added) + "\n")
    backup.save()


def guide_restore(rollback: Path, encrypted: bool):
    console.print("\n" + "─" * 60)
    console.print("[bold yellow]RESTORE THE BACKUP TO YOUR IPHONE[/bold yellow]")
    console.print("─" * 60)
    console.print(
        "\n  1. On the iPhone: Settings → [your name] → Find My → turn [bold]Find My iPhone[/bold] OFF\n"
        "  2. Finder → select the iPhone → [bold]Restore Backup…[/bold] → pick the backup you just chose"
        + (" → enter the backup password" if encrypted else "") + "\n"
        "  3. Keep the cable plugged in until the iPhone restarts and finishes\n"
        "  4. Open WhatsApp. If it asks you to verify your number, do it.\n"
        "     If it offers to restore from iCloud, tap [bold]Skip[/bold] — iCloud would replace the imported chats.\n"
        "  5. Turn Find My back on.\n\n"
        "[bold]Keep WhatsApp on your Android phone untouched until you have checked your chats on the iPhone.[/bold]\n"
        "Nothing on the Android phone was changed, so it remains your full copy.\n\n"
        f"To undo the backup edit (before restoring):  [bold]bash \"{rollback}\"[/bold]"
    )
    console.print("─" * 60)


def restore_whatsapp_only(slim_root: Path, udid: str, password: str | None):
    """Sends the WhatsApp-only backup to the connected iPhone. Nothing outside WhatsApp's chats/media is touched."""
    import asyncio
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.services.mobilebackup2 import Mobilebackup2Service

    async def run():
        lockdown = await create_using_usbmux(serial=udid)
        async with Mobilebackup2Service(lockdown) as svc:
            last = [-10]

            def progress(pct):
                if pct - last[0] >= 10:
                    last[0] = pct
                    console.print(f"    {pct:.0f}%")

            await svc.restore(
                backup_directory=str(slim_root),
                system=False,        # no system files
                reboot=True,         # WhatsApp picks up the new data after restart
                copy=False,
                settings=True,       # RestorePreserveSettings: keep the phone's current settings
                remove=False,        # RemoveItemsNotRestored off: every other app stays as it is
                password=password or "",
                source=udid,
                skip_apps=True,      # don't reinstall apps
                progress_callback=progress,
            )

    asyncio.run(run())
