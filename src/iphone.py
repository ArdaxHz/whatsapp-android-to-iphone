"""
iPhone backup creation, WhatsApp data injection, and guided restore.
Uses the iTunes/Finder backup format directly (no jailbreak needed).
"""
import sqlite3
import hashlib
import shutil
import plistlib
import uuid
import os
import subprocess
from pathlib import Path
from datetime import datetime
from rich.console import Console
from rich.prompt import Confirm

console = Console()

WHATSAPP_BUNDLE = "net.whatsapp.WhatsApp"
WHATSAPP_DOMAIN = f"AppDomain-{WHATSAPP_BUNDLE}"

# Known relative paths inside iOS WhatsApp sandbox
WA_DB_PATH = "Library/Application Support/ChatStorage.sqlite"
WA_MEDIA_PATH = "Message/Media"

BACKUP_BASE = Path.home() / "Library" / "Application Support" / "MobileSync" / "Backup"


def _sha1(domain: str, relative_path: str) -> str:
    """Compute the filename key used in iTunes backup for a given domain+path."""
    return hashlib.sha1(f"{domain}-{relative_path}".encode()).hexdigest()


def find_iphone_backup() -> Path | None:
    """Returns the most recent iPhone backup directory, or None."""
    if not BACKUP_BASE.exists():
        return None
    backups = sorted(
        [d for d in BACKUP_BASE.iterdir() if d.is_dir()],
        key=lambda d: d.stat().st_mtime,
        reverse=True,
    )
    return backups[0] if backups else None


def create_backup() -> Path | None:
    """
    Triggers an iPhone backup via Finder/iTunes.
    Returns path to the backup directory.
    """
    console.print("\n[bold]Creating iPhone backup...[/bold]")
    console.print("  [dim]→ This may open Finder or iTunes on your Mac[/dim]")

    before = set(BACKUP_BASE.glob("*")) if BACKUP_BASE.exists() else set()

    # Use idevicebackup2 if available (libimobiledevice), else fall back to applescript
    if shutil.which("idevicebackup2"):
        console.print("  Using idevicebackup2...")
        result = subprocess.run(
            ["idevicebackup2", "backup", "--full", str(BACKUP_BASE)],
            capture_output=False,
        )
        if result.returncode != 0:
            console.print("[red]  ✗ idevicebackup2 failed.[/red]")
            return None
    else:
        # AppleScript to trigger backup via Finder
        script = """
        tell application "Finder" to activate
        do shell script "open 'itms-backup://'"
        """
        console.print(
            "\n  [yellow]Automatic backup not available.[/yellow]\n"
            "  Please back up your iPhone manually:\n"
            "  1. Connect iPhone to this Mac\n"
            "  2. Open Finder → select your iPhone → click [bold]Back Up Now[/bold]\n"
            "  3. Wait for it to finish\n"
            "  4. Press Enter here when done"
        )
        input()

    after = set(BACKUP_BASE.glob("*")) if BACKUP_BASE.exists() else set()
    new_backups = after - before
    if new_backups:
        backup_path = list(new_backups)[0]
    else:
        backup_path = find_iphone_backup()

    if not backup_path:
        console.print("[red]  ✗ No backup found.[/red]")
        return None

    console.print(f"[green]  ✓ Backup found:[/green] {backup_path.name}")
    return backup_path


def _read_manifest(backup_path: Path) -> sqlite3.Connection:
    manifest_db = backup_path / "Manifest.db"
    conn = sqlite3.connect(str(manifest_db))
    return conn


def inject_whatsapp_data(
    backup_path: Path,
    ios_db: Path,
    media_dir: Path | None,
) -> bool:
    """
    Injects the converted iOS WhatsApp database (and media) into the backup.
    Modifies Manifest.db and copies the file into the backup directory.
    Returns True on success.
    """
    console.print("\n[bold]Injecting WhatsApp data into backup...[/bold]")

    manifest_db_path = backup_path / "Manifest.db"
    if not manifest_db_path.exists():
        console.print("[red]  ✗ Manifest.db not found. Is this a valid backup?[/red]")
        return False

    # Back up original Manifest.db before modifying
    shutil.copy2(str(manifest_db_path), str(manifest_db_path.parent / "Manifest.db.bak"))
    console.print("  [dim]→ Original Manifest.db backed up as Manifest.db.bak[/dim]")

    conn = sqlite3.connect(str(manifest_db_path))

    files_to_inject = [(WA_DB_PATH, ios_db)]

    # Media files
    injected_media = 0
    if media_dir and media_dir.exists():
        console.print("  → Including media files...")
        for media_file in media_dir.rglob("*"):
            if media_file.is_file():
                rel = media_file.relative_to(media_dir)
                wa_rel_path = f"{WA_MEDIA_PATH}/{rel}"
                files_to_inject.append((wa_rel_path, media_file))
                injected_media += 1
        console.print(f"  [dim]→ {injected_media:,} media files queued[/dim]")

    injected = 0
    for rel_path, src_file in files_to_inject:
        file_hash = _sha1(WHATSAPP_DOMAIN, rel_path)
        file_size = src_file.stat().st_size

        # Destination inside backup
        dest_dir = backup_path / file_hash[:2]
        dest_dir.mkdir(exist_ok=True)
        dest_file = dest_dir / file_hash

        shutil.copy2(str(src_file), str(dest_file))

        # Upsert into Manifest.db
        conn.execute(
            """INSERT OR REPLACE INTO Files
               (fileID, domain, relativePath, flags, file)
               VALUES (?, ?, ?, 1, ?)""",
            (
                file_hash,
                WHATSAPP_DOMAIN,
                rel_path,
                _make_file_plist(rel_path, file_size),
            ),
        )
        injected += 1

    conn.commit()
    conn.close()

    console.print(f"[green]  ✓ Injected {injected:,} files into backup[/green]")
    return True


def _make_file_plist(rel_path: str, size: int) -> bytes:
    """Creates the minimal binary plist stored in Manifest.db Files.file column."""
    now = datetime.utcnow()
    data = {
        "$version": 100000,
        "$objects": [
            "$null",
            {
                "$class": {"CF$UID": 2},
                "Birth": 0,
                "EncryptionKey": {"CF$UID": 0},
                "FileID": "",
                "Flags": 1,
                "GroupID": 501,
                "InodeNumber": 0,
                "LastModified": now,
                "LastStatusChange": now,
                "Mode": 33188,
                "ProtectionClass": 0,
                "RelativePath": rel_path,
                "Size": size,
                "UserID": 501,
            },
            {"$classname": "MBFile", "$classes": ["MBFile", "NSObject"]},
        ],
        "$archiver": "NSKeyedArchiver",
        "$top": {"root": {"CF$UID": 1}},
    }
    return plistlib.dumps(data, fmt=plistlib.FMT_BINARY)


def guide_restore(backup_path: Path):
    """Prints clear instructions for restoring the modified backup."""
    console.print("\n" + "─" * 60)
    console.print("[bold yellow]RESTORE INSTRUCTIONS[/bold yellow]")
    console.print("─" * 60)
    console.print(
        "\nYour modified backup is ready at:\n"
        f"  [bold]{backup_path}[/bold]\n\n"
        "To restore it to your iPhone:\n\n"
        "  1. Open [bold]Finder[/bold] on your Mac\n"
        "  2. Select your iPhone in the sidebar\n"
        "  3. Click [bold]Restore Backup...[/bold]\n"
        "  4. Choose the backup listed above\n"
        "  5. Click [bold]Restore[/bold]\n"
        "  6. Wait — do NOT unplug the iPhone until it fully restarts\n\n"
        "[yellow]⚠ If WhatsApp shows no chats after restore:[/yellow]\n"
        "  • Open WhatsApp → it may prompt 'Restore chat history' → tap Restore\n"
        "  • If still empty: restore from Manifest.db.bak (the original backup)\n"
    )
    console.print("─" * 60)
