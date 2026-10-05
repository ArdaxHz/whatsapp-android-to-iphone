"""
Edits WhatsApp's ChatStorage.sqlite inside a local (unencrypted) Finder backup of the iPhone.

Only three files in the backup are touched (ChatStorage.sqlite, its -wal/-shm) plus
Manifest.db. Originals of all four are copied aside first, with a rollback script.
"""
import hashlib
import plistlib
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from rich.console import Console
from rich.prompt import Prompt

console = Console()

WA_DOMAIN = "AppDomainGroup-group.net.whatsapp.WhatsApp.shared"
CHATSTORAGE = "ChatStorage.sqlite"
BACKUP_BASE = Path.home() / "Library" / "Application Support" / "MobileSync" / "Backup"


def file_id(rel_path: str) -> str:
    return hashlib.sha1(f"{WA_DOMAIN}-{rel_path}".encode()).hexdigest()


def blob_path(backup: Path, fid: str) -> Path:
    return backup / fid[:2] / fid


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
    pick = Prompt.ask("Which backup is your iPhone's NEW backup?", choices=[str(n) for n in range(1, len(backups) + 1)], default="1")
    return backups[int(pick) - 1]


def check_backup(backup: Path) -> bool:
    manifest = plistlib.loads((backup / "Manifest.plist").read_bytes())
    if manifest.get("IsEncrypted"):
        console.print(
            "[red]This backup is encrypted, which this tool cannot edit.[/red]\n"
            "In Finder, untick [bold]Encrypt local backup[/bold], click Back Up Now, then run again.\n"
            "[dim](An unencrypted backup does not carry saved passwords, Health data or Wi-Fi networks — "
            "those stay in iCloud Keychain/iCloud if you use it.)[/dim]"
        )
        return False
    with sqlite3.connect(backup / "Manifest.db") as conn:
        found = conn.execute(
            "SELECT COUNT(*) FROM Files WHERE domain = ? AND relativePath = ?", (WA_DOMAIN, CHATSTORAGE)
        ).fetchone()[0]
    if not found:
        console.print(
            "[red]WhatsApp's chat database is not in this backup.[/red]\n"
            "Install WhatsApp on the iPhone, verify your number, open it once, then back up again."
        )
        return False
    return True


def save_originals(backup: Path, safe_dir: Path):
    """Copies every file we will touch, and writes rollback.sh that puts them back."""
    safe_dir.mkdir(parents=True, exist_ok=True)
    lines = ["#!/bin/bash", "# Puts the iPhone backup back exactly as it was before migration.", "set -e"]
    for name in ("Manifest.db", "Manifest.db-wal", "Manifest.db-shm"):
        src = backup / name
        if src.exists():
            shutil.copy2(src, safe_dir / name)
            lines.append(f'cp "{safe_dir / name}" "{src}"')
    for suffix in ("", "-wal", "-shm"):
        fid = file_id(CHATSTORAGE + suffix)
        src = blob_path(backup, fid)
        if src.exists():
            shutil.copy2(src, safe_dir / fid)
            lines.append(f'cp "{safe_dir / fid}" "{src}"')
    rollback = safe_dir / "rollback.sh"
    rollback.write_text("\n".join(lines) + "\necho 'Backup restored to original.'\n")
    rollback.chmod(0o755)
    return rollback


def extract_chatstorage(backup: Path, work_dir: Path) -> Path:
    """Copies ChatStorage (+ its WAL) out and folds the WAL in, giving one self-contained file."""
    work_dir.mkdir(parents=True, exist_ok=True)
    db = work_dir / CHATSTORAGE
    for suffix in ("", "-wal", "-shm"):
        (work_dir / (CHATSTORAGE + suffix)).unlink(missing_ok=True)
        src = blob_path(backup, file_id(CHATSTORAGE + suffix))
        if src.exists() and suffix != "-shm":
            shutil.copy2(src, work_dir / (CHATSTORAGE + suffix))
    conn = sqlite3.connect(db)
    if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        conn.close()
        raise RuntimeError("The iPhone's WhatsApp database in this backup is damaged. Make a fresh backup.")
    conn.execute("PRAGMA journal_mode=DELETE")  # merges -wal into the main file
    conn.close()
    return db


def _set_size(blob: bytes, size: int) -> bytes:
    plist = plistlib.loads(blob)
    for obj in plist["$objects"]:
        if isinstance(obj, dict) and "Size" in obj:
            obj["Size"] = size
            obj["LastModified"] = int(datetime.now().timestamp())
            return plistlib.dumps(plist, fmt=plistlib.FMT_BINARY)
    raise RuntimeError("Unexpected Manifest.db file record format.")


def write_back(backup: Path, new_db: Path):
    """Replaces ChatStorage in the backup and empties its stale -wal/-shm, updating Manifest sizes."""
    conn = sqlite3.connect(backup / "Manifest.db")
    for suffix in ("", "-wal", "-shm"):
        fid = file_id(CHATSTORAGE + suffix)
        row = conn.execute("SELECT file FROM Files WHERE fileID = ?", (fid,)).fetchone()
        if not row:
            continue
        dest = blob_path(backup, fid)
        dest.parent.mkdir(exist_ok=True)
        if suffix:
            # An old WAL replayed over the new database would corrupt it — leave an empty one.
            dest.write_bytes(b"")
        else:
            shutil.copyfile(new_db, dest)
        conn.execute("UPDATE Files SET file = ? WHERE fileID = ?", (_set_size(row[0], dest.stat().st_size), fid))
    conn.commit()
    conn.close()


def guide_restore(rollback: Path):
    console.print("\n" + "─" * 60)
    console.print("[bold yellow]RESTORE THE BACKUP TO YOUR IPHONE[/bold yellow]")
    console.print("─" * 60)
    console.print(
        "\n  1. On the iPhone: Settings → [your name] → Find My → turn [bold]Find My iPhone[/bold] OFF\n"
        "  2. Finder → select the iPhone → [bold]Restore Backup…[/bold] → pick the backup you just chose\n"
        "  3. Keep the cable plugged in until the iPhone restarts and finishes\n"
        "  4. Open WhatsApp. If it asks you to verify your number, do it.\n"
        "     If it offers to restore from iCloud, tap [bold]Skip[/bold] — iCloud would replace the imported chats.\n"
        "  5. Turn Find My back on.\n\n"
        "[bold]Keep WhatsApp on your Android phone untouched until you have checked your chats on the iPhone.[/bold]\n"
        "Nothing on the Android phone was changed, so it remains your full copy.\n\n"
        f"To undo the backup edit (before restoring):  [bold]bash \"{rollback}\"[/bold]"
    )
    console.print("─" * 60)
