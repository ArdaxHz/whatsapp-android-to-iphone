"""
Android extraction via ADB — pulls WhatsApp backups and media.
"""
import subprocess
import shutil
import os
import tarfile
import io
import zlib
from pathlib import Path
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn

console = Console()


def run_adb(args: list, check=True, capture=True) -> subprocess.CompletedProcess:
    cmd = ["adb"] + args
    return subprocess.run(
        cmd,
        capture_output=capture,
        text=True,
        check=check,
    )


def check_device() -> str | None:
    """Returns device serial if connected, None otherwise."""
    result = run_adb(["devices"], check=False)
    lines = result.stdout.strip().splitlines()
    devices = [l for l in lines[1:] if "\tdevice" in l]
    if not devices:
        return None
    return devices[0].split("\t")[0]


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


def pull_whatsapp_files(output_dir: Path) -> dict:
    """
    Pulls WhatsApp backup DB and media from external storage.
    No root required — these live on the public SD card area.
    Returns paths to pulled files.
    """
    result = {}

    db_dir = output_dir / "databases"
    media_dir = output_dir / "media"
    db_dir.mkdir(parents=True, exist_ok=True)
    media_dir.mkdir(parents=True, exist_ok=True)

    console.print("\n[bold]Step 1/3:[/bold] Pulling WhatsApp backup database...")
    db_pull = run_adb(
        ["pull", "/sdcard/WhatsApp/Databases/", str(db_dir)],
        check=False,
    )
    if db_pull.returncode == 0 or (db_dir / "Databases").exists():
        # adb pull of a dir sometimes nests it
        nested = db_dir / "Databases"
        if nested.exists():
            for f in nested.iterdir():
                shutil.move(str(f), str(db_dir / f.name))
            nested.rmdir()
        crypt_files = list(db_dir.glob("msgstore*.crypt*"))
        if crypt_files:
            result["backup_db"] = sorted(crypt_files)[-1]  # latest
            console.print(f"[green]  ✓ Got backup:[/green] {result['backup_db'].name}")
        else:
            console.print("[yellow]  ⚠ No msgstore backup found on SD card.[/yellow]")
    else:
        console.print(f"[yellow]  ⚠ Could not pull databases: {db_pull.stderr.strip()}[/yellow]")

    console.print("\n[bold]Step 2/3:[/bold] Pulling WhatsApp media (this may take a while)...")
    media_pull = run_adb(
        ["pull", "/sdcard/WhatsApp/Media/", str(media_dir)],
        check=False,
    )
    if media_pull.returncode == 0:
        console.print(f"[green]  ✓ Media pulled to:[/green] {media_dir}")
        result["media_dir"] = media_dir
    else:
        console.print(f"[yellow]  ⚠ Media pull issue: {media_pull.stderr.strip()}[/yellow]")

    return result


def attempt_key_via_adb_backup(output_dir: Path) -> Path | None:
    """
    Attempts to extract the WhatsApp key file via `adb backup`.
    Works on Android < 9 or custom ROMs — fails silently on modern devices.
    Returns path to key file if successful, None otherwise.
    """
    console.print("\n[bold]Step 3/3:[/bold] Attempting key extraction via ADB backup...")
    console.print("  [dim]→ Your phone may show a backup confirmation screen — tap Back Up My Data[/dim]")

    ab_path = output_dir / "whatsapp.ab"
    result = run_adb(
        ["backup", "-f", str(ab_path), "-noapk", "com.whatsapp"],
        check=False,
        capture=False,
    )

    if not ab_path.exists() or ab_path.stat().st_size < 100:
        console.print("  [yellow]⚠ ADB backup not available on this device (common on Android 10+).[/yellow]")
        return None

    try:
        with open(ab_path, "rb") as f:
            raw = f.read()

        # ADB backup format: 24-byte header + zlib-compressed tar
        header = raw[:24]
        if b"android backup" not in header.lower():
            return None

        compressed = raw[24:]
        try:
            decompressed = zlib.decompress(compressed)
        except zlib.error:
            # Some backups use raw deflate
            import zlib as _z
            decompressed = _z.decompress(compressed, -15)

        tar_buf = io.BytesIO(decompressed)
        with tarfile.open(fileobj=tar_buf) as tar:
            for member in tar.getmembers():
                if member.name.endswith("/files/key"):
                    f_obj = tar.extractfile(member)
                    if f_obj:
                        key_path = output_dir / "whatsapp.key"
                        key_path.write_bytes(f_obj.read())
                        console.print(f"[green]  ✓ Key extracted via ADB backup![/green]")
                        return key_path
    except Exception as e:
        console.print(f"  [yellow]⚠ ADB backup extraction failed: {e}[/yellow]")

    return None


def attempt_key_via_run_as(output_dir: Path) -> Path | None:
    """
    Tries `adb shell run-as com.whatsapp` — only works on debug builds (rare).
    """
    result = run_adb(
        ["shell", "run-as", "com.whatsapp", "cat", "/data/data/com.whatsapp/files/key"],
        check=False,
    )
    if result.returncode == 0 and len(result.stdout) > 10:
        key_path = output_dir / "whatsapp.key"
        key_path.write_bytes(result.stdout.encode("latin-1"))
        console.print("[green]  ✓ Key extracted via run-as![/green]")
        return key_path
    return None
