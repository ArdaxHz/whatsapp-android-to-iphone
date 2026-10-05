"""
Android extraction via ADB — pulls WhatsApp backups and media. Read-only: nothing on the phone is changed.
"""
import subprocess
from pathlib import Path
from rich.console import Console

console = Console()

# Android 11+ uses scoped storage; older versions use /sdcard/WhatsApp.
WA_ROOTS = [
    "/sdcard/Android/media/com.whatsapp/WhatsApp",
    "/sdcard/WhatsApp",
]


def run_adb(args: list, capture=True) -> subprocess.CompletedProcess:
    return subprocess.run(["adb"] + args, capture_output=capture, text=True)


def check_device() -> str | None:
    """Returns device serial if connected, None otherwise."""
    lines = run_adb(["devices"]).stdout.strip().splitlines()
    devices = [l for l in lines[1:] if l.endswith("\tdevice")]
    return devices[0].split("\t")[0] if devices else None


def ensure_device():
    serial = check_device()
    if not serial:
        console.print("\n[red]No Android device detected.[/red]")
        console.print("Make sure:")
        console.print("  1. USB cable is connected")
        console.print("  2. USB Debugging is ON (Settings → Developer Options → USB Debugging)")
        console.print("  3. You tapped [bold]Allow[/bold] on the 'Allow USB debugging?' popup on your phone")
        console.print("\nThen press Enter to retry...")
        input()
        serial = check_device()
        if not serial:
            raise RuntimeError("Still no device. Check the USB connection and try again.")
    console.print(f"[green]Android device connected:[/green] {serial}")
    return serial


def _find_wa_root() -> str | None:
    for root in WA_ROOTS:
        if run_adb(["shell", "ls", f"{root}/Databases"]).returncode == 0:
            return root
    return None


def pull_whatsapp_files(output_dir: Path) -> dict:
    """Pulls the newest msgstore backup and the Media folder. Returns paths to pulled files."""
    result = {}
    root = _find_wa_root()
    if not root:
        console.print("[yellow]  ⚠ WhatsApp folder not found on the phone.[/yellow]")
        return result
    console.print(f"  [dim]WhatsApp folder: {root}[/dim]")

    db_dir = output_dir / "databases"
    db_dir.mkdir(parents=True, exist_ok=True)

    # Newest file first — msgstore.db.crypt15 is the latest, dated ones are older copies.
    ls = run_adb(["shell", "ls", "-t", f"{root}/Databases"])
    names = [n.strip() for n in ls.stdout.split() if n.strip().startswith("msgstore") and ".crypt" in n]
    if not names:
        console.print("[yellow]  ⚠ No msgstore backup found on the phone.[/yellow]")
        return result

    console.print(f"\n[bold]Pulling backup database[/bold] {names[0]}...")
    dest = db_dir / names[0]
    pull = run_adb(["pull", f"{root}/Databases/{names[0]}", str(dest)])
    if pull.returncode != 0 or not dest.exists():
        console.print(f"[red]  ✗ Could not pull database: {pull.stderr.strip()}[/red]")
        return result
    result["backup_db"] = dest
    console.print(f"[green]  ✓ Got backup:[/green] {dest.name}")

    console.print("\n[bold]Pulling WhatsApp media[/bold] (a copy is kept on your Mac; this may take a while)...")
    media_dir = output_dir / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    pull = run_adb(["pull", f"{root}/Media/.", str(media_dir)], capture=False)
    if pull.returncode == 0:
        console.print(f"[green]  ✓ Media copied to:[/green] {media_dir}")
        result["media_dir"] = media_dir
    else:
        console.print("[yellow]  ⚠ Some media could not be pulled (messages are unaffected).[/yellow]")

    return result


def attempt_key_via_root(output_dir: Path) -> Path | None:
    """Reads the local backup key on rooted phones. Fails quietly on normal phones."""
    r = subprocess.run(
        ["adb", "exec-out", "su", "-c", "cat /data/data/com.whatsapp/files/key"],
        capture_output=True,
    )
    if r.returncode == 0 and len(r.stdout) > 100:
        key_path = output_dir / "whatsapp.key"
        key_path.write_bytes(r.stdout)
        console.print("[green]  ✓ Key extracted (rooted phone).[/green]")
        return key_path
    return None
