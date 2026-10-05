#!/usr/bin/env python3
"""
WhatsApp Android → iPhone Migration Tool
-----------------------------------------
Pulls WhatsApp's backup from Android via ADB, decrypts it, merges the chats into
the WhatsApp database inside a Finder backup of your iPhone, and guides the restore.

Nothing on the Android phone is changed. The original backup files are saved aside
with a rollback script before anything is edited.
"""
import sys
from datetime import datetime
from pathlib import Path

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.prompt import Prompt, Confirm
except ImportError:
    print("Missing dependencies. Run:  bash setup.sh")
    sys.exit(1)

console = Console()

OUTPUT_DIR = Path.home() / "WhatsApp-Migration"


def banner():
    console.print(Panel.fit(
        "[bold white]WhatsApp Android → iPhone Migration[/bold white]\n"
        "[dim]No factory reset. Your Android phone is never modified.[/dim]",
        border_style="green",
    ))
    console.print()


def check_deps():
    import importlib
    import shutil
    missing = []
    for pkg in ["wa_crypt_tools", "rich"]:
        try:
            importlib.import_module(pkg)
        except ImportError:
            missing.append(pkg)
    if missing:
        console.print(f"[red]Missing packages:[/red] {', '.join(missing)}")
        console.print("Run:  [bold]bash setup.sh[/bold]")
        sys.exit(1)
    if not shutil.which("adb"):
        console.print("[red]adb not found.[/red] Run:  [bold]brew install android-platform-tools[/bold]")
        sys.exit(1)


def step_android(dirs: dict) -> dict:
    from src.android import ensure_device, pull_whatsapp_files, attempt_key_via_root

    console.print(Panel("[bold]STEP 1 — Get the backup from Android[/bold]", style="blue"))
    console.print(
        "\nOn the Android phone, set up a backup this tool can decrypt:\n"
        "  1. WhatsApp → Settings → Chats → Chat backup → [bold]End-to-end encrypted backup[/bold] → Turn on\n"
        "  2. Choose [bold]Use 64-digit encryption key instead[/bold] (a password or passkey will NOT work —\n"
        "     WhatsApp keeps those keys on its servers / your Google account, not on the phone)\n"
        "  3. [bold]Write the 64-digit key down[/bold], then tap BACK UP NOW and wait for it to finish\n"
        "  4. Enable USB Debugging (Settings → About Phone → tap Build Number 7×,\n"
        "     then Developer Options → USB Debugging) and connect the phone\n"
        "[dim]Already using a password or passkey? Turn E2E backup off, then on again with the 64-digit key,\n"
        "and do this BEFORE registering WhatsApp on the iPhone (that logs Android out).[/dim]\n"
    )
    if not Confirm.ask("Done and phone connected?", default=True):
        sys.exit(0)

    ensure_device()
    pulled = pull_whatsapp_files(dirs["android"])
    if "backup_db" not in pulled:
        console.print("Open WhatsApp → Settings → Chats → Chat backup → BACK UP NOW, then run again.")
        sys.exit(1)
    pulled["key"] = attempt_key_via_root(dirs["android"])
    return pulled


def step_decrypt(dirs: dict, pulled: dict) -> Path:
    from src.decrypt import decrypt_backup, normalize_hex_key

    console.print(Panel("[bold]STEP 2 — Decrypt[/bold]", style="blue"))
    key = pulled.get("key")
    if key:
        db = decrypt_backup(pulled["backup_db"], key, dirs["decrypted"])
        if db:
            return db
    for _ in range(3):
        key = normalize_hex_key(Prompt.ask("Enter the 64-digit backup key"))
        if not key:
            console.print("[yellow]That isn't 64 digits/letters (0-9, a-f). Try again.[/yellow]")
            continue
        db = decrypt_backup(pulled["backup_db"], key, dirs["decrypted"])
        if db:
            return db
        console.print(
            "[yellow]Wrong key, or the backup on the phone was made before you set this key.[/yellow]\n"
            "[dim]If so, tap BACK UP NOW on Android and restart this tool.[/dim]"
        )
    console.print("[red]Could not decrypt. Nothing was changed anywhere.[/red]")
    sys.exit(1)


def step_iphone(dirs: dict, android_db: Path, media_dir: Path | None):
    from src.iphone import choose_backup, open_backup, save_originals, extract_chatstorage, write_back, guide_restore
    from src.convert import merge

    console.print(Panel("[bold]STEP 3 — Merge into your iPhone backup[/bold]", style="blue"))
    console.print(
        "\nOn the iPhone, WhatsApp must be installed and registered with the [bold]same number[/bold].\n"
        "Then make a fresh local backup:\n"
        "  1. Connect the iPhone, tap [bold]Trust[/bold] if asked\n"
        "  2. Finder → select the iPhone → General → [bold]Back up all of the data on your iPhone to this Mac[/bold]\n"
        "  3. Click [bold]Back Up Now[/bold] and wait for it to finish\n"
        "  [dim](Encrypted or not both work — if encrypted you'll be asked for its password.)[/dim]\n"
    )
    if not Confirm.ask("Backup finished?", default=True):
        sys.exit(0)

    path = choose_backup()
    backup = open_backup(path, dirs["ios"] / "manifest") if path else None
    if not backup:
        sys.exit(1)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_dir = dirs["root"] / "iphone_originals" / f"{path.name}-{stamp}"
    rollback = save_originals(path, safe_dir)
    console.print(f"[green]  ✓ Original backup files saved.[/green] Undo script: {rollback}")

    db = extract_chatstorage(backup, dirs["ios"])
    console.print("\n[bold]Merging chats...[/bold]")
    stats = merge(android_db, db, media_dir)

    import sqlite3
    with sqlite3.connect(db) as conn:
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            console.print("[red]Merged database failed its check. Backup NOT modified.[/red]")
            sys.exit(1)

    console.print(
        f"[green]  ✓ {stats['messages']:,} messages imported[/green] "
        f"({len(stats['media']):,} with media; {stats['chats_new']} new chats, "
        f"{stats['chats_merged']} merged into existing, {stats['skipped_dupes']:,} already present)"
    )
    write_back(backup, db, stats["media"], safe_dir)
    console.print("[green]  ✓ iPhone backup updated[/green]")
    step_restore(dirs, backup, path, rollback)


def step_restore(dirs: dict, backup, path: Path, rollback: Path):
    from src.iphone import guide_restore, restore_whatsapp_only

    console.print(Panel("[bold]STEP 4 — Put it on the iPhone[/bold]", style="blue"))
    console.print(
        "  [bold]1[/bold]. WhatsApp only [green](recommended)[/green] — restores just WhatsApp's chats and media.\n"
        "     Other apps, logins, photos and settings are left exactly as they are.\n"
        "  [bold]2[/bold]. Full restore in Finder — restores the whole iPhone from the backup.\n"
    )
    if Prompt.ask("Choose", choices=["1", "2"], default="1") == "2":
        guide_restore(rollback, backup.encrypted)
        return

    console.print("\n[dim]Building WhatsApp-only backup...[/dim]")
    slim_root = backup.export_whatsapp_only(dirs["root"] / "whatsapp_only_restore")
    console.print(
        "\nOn the iPhone, before continuing:\n"
        "  1. Settings → [your name] → Find My → turn [bold]Find My iPhone OFF[/bold] (Apple requires this for any restore)\n"
        "  2. Turn on [bold]Airplane Mode[/bold] (stops WhatsApp writing new messages mid-restore)\n"
        "  3. Swipe WhatsApp away in the app switcher so it's fully closed\n"
        "  4. Keep the iPhone unlocked and plugged in to this Mac\n"
    )
    if not Confirm.ask("Ready?", default=True):
        console.print(f"Nothing sent to the iPhone. Run again any time; undo script: {rollback}")
        return
    try:
        console.print("[bold]Restoring WhatsApp data...[/bold] (the iPhone shows 'Restore in Progress', then restarts)")
        restore_whatsapp_only(slim_root, path.name, backup.password)
    except Exception as e:
        console.print(f"[red]WhatsApp-only restore failed:[/red] {e}")
        console.print(
            "Other apps aren't affected (this only ever sends WhatsApp files). If WhatsApp misbehaves now,\n"
            "use the full Finder restore below — your backup already contains everything:"
        )
        guide_restore(rollback, backup.encrypted)
        return
    console.print(
        "\n[green]✓ Done.[/green] When the iPhone has restarted:\n"
        "  • Turn Airplane Mode off, open WhatsApp and check your chats\n"
        "  • If WhatsApp offers an iCloud restore, tap [bold]Skip[/bold]\n"
        "  • Turn Find My back on\n"
        "[bold]Keep WhatsApp on Android untouched until you've checked everything.[/bold]"
    )


def main():
    banner()
    check_deps()
    console.print(f"[dim]Working files are saved to: {OUTPUT_DIR}[/dim]\n")

    dirs = {"root": OUTPUT_DIR, "android": OUTPUT_DIR / "android_data",
            "decrypted": OUTPUT_DIR / "decrypted", "ios": OUTPUT_DIR / "ios_data"}
    for d in dirs.values():
        d.mkdir(parents=True, exist_ok=True)

    android_db = dirs["decrypted"] / "msgstore.db"
    media_dir = dirs["android"] / "media"
    if android_db.exists() and Confirm.ask(
        "Android data from a previous run was found on this Mac. Reuse it (skips Android steps)?", default=True
    ):
        step_iphone(dirs, android_db, media_dir if media_dir.exists() else None)
        return

    pulled = step_android(dirs)
    android_db = step_decrypt(dirs, pulled)
    step_iphone(dirs, android_db, pulled.get("media_dir"))


if __name__ == "__main__":
    main()
