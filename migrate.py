#!/usr/bin/env python3
"""
WhatsApp Android → iPhone Migration Tool
-----------------------------------------
Extracts WhatsApp data from Android via ADB, converts it,
injects it into your iPhone backup, and guides you through restore.

Your iPhone is NEVER factory reset. If anything fails, your original
backup is preserved and can be restored.
"""
import sys
import shutil
from pathlib import Path

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.prompt import Prompt, Confirm
    from rich import print as rprint
except ImportError:
    print("Missing dependencies. Run:  bash setup.sh")
    sys.exit(1)

console = Console()

OUTPUT_DIR = Path.home() / "WhatsApp-Migration"


def banner():
    console.print(Panel.fit(
        "[bold white]WhatsApp Android → iPhone Migration[/bold white]\n"
        "[dim]No factory reset. Your iPhone data is safe.[/dim]",
        border_style="green",
    ))
    console.print()


def check_deps():
    import importlib
    missing = []
    for pkg in ["wa_crypt_tools", "rich", "Crypto"]:
        try:
            importlib.import_module(pkg)
        except ImportError:
            missing.append(pkg)
    if missing:
        console.print(f"[red]Missing packages:[/red] {', '.join(missing)}")
        console.print("Run:  [bold]bash setup.sh[/bold]")
        sys.exit(1)


def setup_dirs() -> dict:
    dirs = {
        "root":      OUTPUT_DIR,
        "android":   OUTPUT_DIR / "android_data",
        "decrypted": OUTPUT_DIR / "decrypted",
        "ios":       OUTPUT_DIR / "ios_data",
    }
    for d in dirs.values():
        d.mkdir(parents=True, exist_ok=True)
    return dirs


def step_android_extraction(dirs: dict) -> dict:
    from src.android import ensure_device, pull_whatsapp_files, attempt_key_via_adb_backup, attempt_key_via_run_as

    console.print(Panel("[bold]STEP 1 — Extract data from Android[/bold]", style="blue"))

    console.print("\nBefore continuing, make sure:")
    console.print("  • Your Android phone is connected via USB")
    console.print("  • USB Debugging is enabled")
    console.print("    [dim](Settings → About Phone → tap Build Number 7 times → Developer Options → USB Debugging)[/dim]")
    console.print("  • WhatsApp is installed and has at least one local backup")
    console.print("    [dim](Open WhatsApp → Settings → Chats → Chat Backup → BACK UP NOW)[/dim]")
    console.print()

    if not Confirm.ask("Ready to connect Android?", default=True):
        console.print("[yellow]Cancelled.[/yellow]")
        sys.exit(0)

    ensure_device()

    pulled = pull_whatsapp_files(dirs["android"])

    if "backup_db" not in pulled:
        console.print("\n[red]No WhatsApp backup database found on Android.[/red]")
        console.print("Please open WhatsApp → Settings → Chats → Chat Backup → tap BACK UP NOW")
        console.print("Then run this tool again.")
        sys.exit(1)

    # Try to get decryption key
    key_file = attempt_key_via_run_as(dirs["android"])
    if not key_file:
        key_file = attempt_key_via_adb_backup(dirs["android"])

    if not key_file:
        console.print("\n[yellow]Automatic key extraction did not work on your device.[/yellow]")
        console.print("This is normal for most modern Android phones.")
        console.print("\nYou have two options:\n")
        console.print("  [bold]Option A[/bold] — Use WhatsApp E2E encrypted backup password/key")
        console.print("    Open WhatsApp → Settings → Chats → Chat backup → End-to-end encrypted backup")
        console.print("    Turn it ON with a password. Then enter that password here.\n")
        console.print("  [bold]Option B[/bold] — Skip database (migrate media only)")
        console.print("    Your photos/videos will be migrated but chat text history won't.\n")

        choice = Prompt.ask("Enter your E2E backup password (or press Enter to skip)", default="")
        if choice:
            pulled["e2e_password"] = choice
        else:
            console.print("[yellow]Continuing with media-only migration.[/yellow]")
            pulled["media_only"] = True
    else:
        pulled["key_file"] = key_file

    return pulled


def step_decrypt(dirs: dict, pulled: dict) -> Path | None:
    from src.decrypt import decrypt_backup, decrypt_with_password

    if pulled.get("media_only"):
        return None

    console.print(Panel("[bold]STEP 2 — Decrypt WhatsApp database[/bold]", style="blue"))

    crypt_file: Path = pulled["backup_db"]
    key_file: Path | None = pulled.get("key_file")
    password: str | None = pulled.get("e2e_password")

    decrypted_db = None
    if key_file:
        decrypted_db = decrypt_backup(crypt_file, key_file, dirs["decrypted"])
    elif password:
        decrypted_db = decrypt_with_password(crypt_file, password, dirs["decrypted"])

    if not decrypted_db:
        console.print("[red]Could not decrypt the database.[/red]")
        console.print(
            "If you set up an E2E encrypted backup, make sure the password is correct.\n"
            "Otherwise, proceed with media-only migration (your photos/videos will be migrated)."
        )
        if not Confirm.ask("Continue with media-only migration?", default=True):
            sys.exit(0)
        return None

    return decrypted_db


def step_convert(dirs: dict, android_db: Path) -> Path:
    from src.convert import convert

    console.print(Panel("[bold]STEP 3 — Convert to iOS format[/bold]", style="blue"))

    ios_db = dirs["ios"] / "ChatStorage.sqlite"
    convert(android_db, ios_db)
    return ios_db


def step_iphone_backup(dirs: dict, ios_db: Path | None, media_dir: Path | None):
    from src.iphone import create_backup, inject_whatsapp_data, guide_restore, find_iphone_backup

    console.print(Panel("[bold]STEP 4 — Backup & inject into iPhone[/bold]", style="blue"))

    console.print("\nNow connect your iPhone via USB.")
    console.print("Make sure you have [bold]trusted this Mac[/bold] on your iPhone.")
    console.print("  [dim](A popup 'Trust This Computer?' should appear — tap Trust)[/dim]\n")

    if not Confirm.ask("iPhone connected and trusted?", default=True):
        console.print("[yellow]Cancelled.[/yellow]")
        sys.exit(0)

    # Create backup
    backup_path = create_backup()
    if not backup_path:
        console.print("[red]Could not create iPhone backup. Make sure iPhone is connected and trusted.[/red]")
        sys.exit(1)

    if ios_db is None and media_dir is None:
        console.print("[yellow]Nothing to inject — no database and no media available.[/yellow]")
        sys.exit(1)

    # Inject
    success = inject_whatsapp_data(
        backup_path,
        ios_db,
        media_dir,
    )

    if not success:
        console.print("[red]Injection failed. Your iPhone backup is untouched (not restored).[/red]")
        sys.exit(1)

    # Guide restore
    guide_restore(backup_path)


def main():
    banner()
    check_deps()

    console.print(f"[dim]All extracted data will be saved to: {OUTPUT_DIR}[/dim]\n")

    dirs = setup_dirs()

    # Step 1: Android extraction
    pulled = step_android_extraction(dirs)

    media_dir = pulled.get("media_dir")

    # Step 2: Decrypt
    android_db = step_decrypt(dirs, pulled)

    # Step 3: Convert (only if we have the DB)
    ios_db = None
    if android_db:
        ios_db = step_convert(dirs, android_db)
    else:
        console.print("\n[yellow]Skipping database conversion (no decrypted DB available).[/yellow]")

    # Step 4: iPhone backup + inject + restore guide
    step_iphone_backup(dirs, ios_db, media_dir)

    console.print("\n[bold green]Migration complete![/bold green]")
    console.print("Follow the restore instructions above, then check WhatsApp on your iPhone.")
    console.print(f"\nAll files saved to: [bold]{OUTPUT_DIR}[/bold]")


if __name__ == "__main__":
    main()
