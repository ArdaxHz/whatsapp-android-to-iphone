"""
Decrypts WhatsApp crypt12/14/15 backup files with wa-crypt-tools.
"""
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from rich.console import Console

console = Console()


def normalize_hex_key(text: str) -> str | None:
    """The 64-digit E2E key as shown by WhatsApp (spaces/dashes allowed) → hex string, else None."""
    key = re.sub(r"[\s-]", "", text).lower()
    return key if re.fullmatch(r"[0-9a-f]{64}", key) else None


def decrypt_backup(crypt_file: Path, key: str | Path, output_dir: Path) -> Path | None:
    """key is a key file path or a 64-digit hex key. Returns the decrypted msgstore.db or None."""
    out_db = output_dir / "msgstore.db"
    out_db.unlink(missing_ok=True)
    console.print(f"\n[bold]Decrypting[/bold] {crypt_file.name}...")

    result = subprocess.run(
        [sys.executable, "-m", "wa_crypt_tools.wadecrypt", str(key), str(crypt_file), str(out_db)],
        capture_output=True,
        text=True,
    )

    if result.returncode == 0 and out_db.exists():
        try:
            conn = sqlite3.connect(out_db)
            ok = conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            count = conn.execute("SELECT COUNT(*) FROM message").fetchone()[0]
            conn.close()
        except sqlite3.Error as e:
            ok, count = False, str(e)
        if ok:
            console.print(f"[green]  ✓ Decrypted — {count:,} messages found[/green]")
            return out_db
        console.print(f"[red]  ✗ Decrypted file is not a usable WhatsApp database ({count}).[/red]")
        return None

    err = (result.stderr or result.stdout or "").strip().splitlines()
    console.print(f"[red]  ✗ Decryption failed:[/red] {err[-1] if err else 'unknown error'}")
    return None
