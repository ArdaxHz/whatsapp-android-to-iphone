# WhatsApp Android → iPhone Migration Tool

Migrate your entire WhatsApp history — chats, groups, media — from Android to an already set-up iPhone. No factory reset required.

> **Your iPhone is never wiped.** The tool works by injecting your WhatsApp data into an iTunes backup. If anything fails, your original backup is preserved and can be restored.

---

## Why this exists

The official WhatsApp migration (Move to iOS) only works during the iPhone's initial setup screen. If your iPhone is already set up, you're stuck — unless you pay $30–40 for a commercial tool.

This is a free, open-source alternative that does the same thing.

---

## What gets migrated

| Data | Migrated |
|------|----------|
| All chat messages (1-on-1 and groups) | ✅ |
| Group chats & group names | ✅ |
| Photos, videos, voice notes | ✅ |
| Documents & files | ✅ |
| Starred messages | ✅ |
| Message timestamps | ✅ |
| Call history | ❌ (WhatsApp does not include this in backups) |

---

## Requirements

| Requirement | Notes |
|-------------|-------|
| Mac (macOS 12+) | Windows support planned |
| Python 3.10+ | `python3 --version` to check |
| ADB (Android Debug Bridge) | `brew install android-platform-tools` |
| Android phone | USB Debugging must be enabled |
| iPhone | Must be trusted on your Mac |
| iTunes or Finder | For creating the iPhone backup |

---

## Installation

```bash
git clone https://github.com/YOUR_USERNAME/whatsapp-android-to-iphone.git
cd whatsapp-android-to-iphone

bash setup.sh
```

That's it. `setup.sh` installs all Python dependencies automatically.

---

## Usage

### Before you start

**On your Android phone:**
1. Open WhatsApp → Settings → Chats → Chat Backup
2. Tap **BACK UP NOW** and wait for it to finish
3. Enable USB Debugging:
   - Settings → About Phone → tap **Build Number** 7 times
   - Settings → Developer Options → turn on **USB Debugging**

**On your iPhone:**
- Make sure you have connected it to this Mac at least once and tapped **Trust** on the popup

### Run the tool

```bash
python3 migrate.py
```

The tool guides you through every step interactively — just follow the prompts.

### What happens

```
STEP 1 — Extract data from Android
  → Pulls WhatsApp backup database (.crypt15) from your phone
  → Pulls all media files (photos, videos, voice notes)
  → Attempts automatic decryption key extraction

STEP 2 — Decrypt the database
  → Decrypts the backup using the extracted key
  → If automatic key extraction fails: asks for your WhatsApp
     E2E encrypted backup password (see Troubleshooting below)

STEP 3 — Convert to iOS format
  → Converts Android's SQLite schema → iOS WhatsApp schema

STEP 4 — Backup & inject into iPhone
  → Creates a full backup of your iPhone (your safety net)
  → Injects the converted WhatsApp data into that backup
  → Guides you through the restore in Finder
```

All extracted files are saved to `~/WhatsApp-Migration/` on your Mac.

---

## Troubleshooting

### "No Android device detected"
- Make sure the USB cable is properly connected
- Check that USB Debugging is ON in Developer Options
- When the **"Allow USB debugging?"** popup appears on your phone, tap **Allow**
- Try a different USB cable (some cables are charge-only)

### "Automatic key extraction did not work"
This is normal on Android 10 and newer. WhatsApp blocks key access on non-rooted devices.

**Fix:** Set up an E2E encrypted backup in WhatsApp:
1. WhatsApp → Settings → Chats → Chat backup
2. Tap **End-to-end encrypted backup** → Turn On
3. Choose a password you'll remember
4. Wait for the encrypted backup to finish
5. Run the tool again — enter that password when prompted

### "WhatsApp shows no chats after restore"
- Open WhatsApp on iPhone — it may show a **"Restore chat history"** prompt → tap Restore
- If still empty: in Finder, restore the original backup (`Manifest.db.bak` was saved automatically)

### "Injection failed"
Your iPhone was not changed at all — the tool exits before restoring if injection fails. Check that:
- The iPhone is trusted on this Mac
- You have enough free space on your Mac for the backup

---

## How it works

```
Android phone                    Your Mac                      iPhone
─────────────                    ────────                      ──────
WhatsApp backup   ──[ADB]──▶   Pull .crypt15 + media
                               Decrypt database
                               Convert schema
                               Create iPhone backup  ◀──[USB]──  iPhone
                               Inject WA data
                               ──[Finder restore]──▶  iPhone gets
                                                       WA data back
```

**Key technical details:**
- WhatsApp Android stores backups as AES-GCM encrypted SQLite databases (`.crypt15` format)
- Decryption uses [wa-crypt-tools](https://github.com/ElDavoo/wa-crypt-tools)
- The Android schema (`msgstore.db`) and iOS schema (`ChatStorage.sqlite`) are completely different — this tool converts between them
- iTunes backups store app data as SHA1-hashed files in `~/Library/Application Support/MobileSync/Backup/`
- The tool injects WhatsApp's app domain (`AppDomain-net.whatsapp.WhatsApp`) into `Manifest.db` and copies the converted files

---

## Project structure

```
├── migrate.py          # Main interactive CLI — start here
├── setup.sh            # One-time dependency installer
├── requirements.txt
└── src/
    ├── android.py      # ADB extraction, key retrieval
    ├── decrypt.py      # crypt12/14/15 decryption
    ├── convert.py      # Android → iOS database schema conversion
    └── iphone.py       # iTunes backup manipulation + restore guide
```

---

## Limitations

- **macOS only** for now (Windows/Linux support is planned — PRs welcome)
- **Non-rooted Android** — automatic key extraction may fail on Android 10+, requiring the E2E backup password workaround
- **No iCloud backup support** — the tool creates a local iTunes/Finder backup, not an iCloud one
- WhatsApp may update its backup format or integrity checks at any time, which could break injection

---

## Contributing

PRs are welcome. Some areas that would make great contributions:

- [ ] Windows support (replace Mac-specific backup path with Windows equivalent)
- [ ] Google Drive key extraction (OAuth flow to retrieve backup key without root)
- [ ] Better media path resolution (match media files to messages)
- [ ] Progress bar improvements for very large media archives
- [ ] Automated tests with sample crypt14/15 files

---

## Disclaimer

This tool is intended for personal use — migrating your own WhatsApp data between your own devices. Do not use it to access anyone else's WhatsApp data. Respect WhatsApp's Terms of Service.

---

## License

MIT License — see [LICENSE](LICENSE) for details.

---

## Acknowledgements

- [wa-crypt-tools](https://github.com/ElDavoo/wa-crypt-tools) by ElDavoo — WhatsApp crypt15 decryption
- [pymobiledevice3](https://github.com/doronz88/pymobiledevice3) by doronz88 — iOS device interaction
