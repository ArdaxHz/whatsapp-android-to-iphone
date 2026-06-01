"""
Decrypts WhatsApp crypt12/14/15 backup files.
Uses wa-crypt-tools under the hood.
"""
import subprocess
import sys
import struct
from pathlib import Path
from rich.console import Console

console = Console()


def decrypt_backup(crypt_file: Path, key_file: Path, output_dir: Path) -> Path | None:
    """
    Decrypts a WhatsApp crypt12/14/15 file into a plain SQLite database.
    Returns path to decrypted .db file, or None on failure.
    Uses wa_crypt_tools.wadecrypt (the correct module name in v0.1.x).
    """
    out_db = output_dir / "msgstore.db"
    suffix = crypt_file.suffix
    console.print(f"\n[bold]Decrypting[/bold] {crypt_file.name} ({suffix})...")

    # wa-crypt-tools v0.1.x entry point is wa_crypt_tools.wadecrypt
    # Args: keyfile encrypted_file decrypted_file
    result = subprocess.run(
        [
            sys.executable, "-m", "wa_crypt_tools.wadecrypt",
            str(key_file),
            str(crypt_file),
            str(out_db),
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode == 0 and out_db.exists() and out_db.stat().st_size > 0:
        console.print("[green]  ✓ Decrypted successfully →[/green] msgstore.db")
        return out_db

    err = (result.stderr or result.stdout or "").strip()
    console.print(f"[red]  ✗ Decryption failed:[/red] {err}")
    return None


def decrypt_with_password(crypt_file: Path, password: str, output_dir: Path) -> Path | None:
    """
    For E2E-encrypted backups set with a 64-digit key or password.
    wa-crypt-tools accepts a hex string key as the first positional argument.
    """
    out_db = output_dir / "msgstore.db"
    console.print("\n[bold]Decrypting with password/key...[/bold]")

    # If password looks like a 64-char hex key, pass it directly.
    # Otherwise pass via --password flag (supported in newer builds).
    args = [sys.executable, "-m", "wa_crypt_tools.wadecrypt"]
    if len(password.replace("-", "").replace(" ", "")) == 64:
        args += [password.replace("-", "").replace(" ", ""), str(crypt_file), str(out_db)]
    else:
        args += ["--password", password, str(crypt_file), str(out_db)]

    result = subprocess.run(args, capture_output=True, text=True)

    if result.returncode == 0 and out_db.exists() and out_db.stat().st_size > 0:
        console.print("[green]  ✓ Decrypted with password![/green]")
        return out_db

    err = (result.stderr or result.stdout or "").strip()
    console.print(f"[red]  ✗ Password decryption failed:[/red] {err}")
    return None


def probe_crypt_version(crypt_file: Path) -> int:
    """Returns the crypt version number (12, 14, 15) from the file header."""
    try:
        with open(crypt_file, "rb") as f:
            header = f.read(32)
        if b"crypt15" in header or header[0:3] == b"\x00\x00\x00":
            return 15
        if b"crypt14" in header:
            return 14
        return 12
    except Exception:
        return 15
