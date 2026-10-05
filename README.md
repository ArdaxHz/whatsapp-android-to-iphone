# WhatsApp Android → iPhone Migration Tool

Move your WhatsApp chat history from Android to an **already set-up iPhone**. No factory reset.

> **Nothing on your Android phone is changed** — it stays your full copy until you've checked the iPhone.
> On the Mac, the original backup files are saved aside with a `rollback.sh` before anything is edited.

---

## What gets migrated

| Data | Migrated |
|------|----------|
| Text messages (1-on-1 and groups), with timestamps and sender | ✅ |
| Group chats, group names, group senders | ✅ |
| Starred messages | ✅ |
| Chats already on the iPhone | ✅ kept, Android history merged in |
| Photos, videos, voice notes, documents | ⚠️ shown as placeholders like `[Photo: IMG-…jpg] caption`; the files are copied to your Mac |
| System notices, deleted messages, status updates | ❌ skipped |

Re-running is safe: messages already imported are skipped.

---

## Requirements

- Mac, Python 3.10+, `adb` (`brew install android-platform-tools`)
- Terminal has **Full Disk Access** (System Settings → Privacy & Security), so it can read Finder backups
- WhatsApp installed and registered on the iPhone with the **same number**

```bash
bash setup.sh
python3 migrate.py
```

---

## Steps (the tool walks you through these)

**1. Android — make a backup the tool can decrypt**
1. WhatsApp → Settings → Chats → Chat backup → **End-to-end encrypted backup** → Turn on
2. Pick **Use 64-digit encryption key instead** and write the key down.
   A *password* won't work: WhatsApp keeps password-protected keys on its servers, so they can't be decrypted offline.
3. Tap **BACK UP NOW** and wait.
4. Enable USB Debugging and plug the phone in.

**2. Decrypt** — enter the 64-digit key. (Rooted phones are read automatically.)

**3. iPhone — make a local backup**
1. Finder → your iPhone → *Back up all of the data on your iPhone to this Mac*
2. Untick **Encrypt local backup**, click **Back Up Now**
3. The tool merges the chats into WhatsApp's database inside that backup.

**4. Restore**
1. Turn off **Find My iPhone**
2. Finder → **Restore Backup…** → the backup you picked
3. Open WhatsApp; verify your number if asked. If it offers an **iCloud** restore, tap **Skip** — that would replace the imported chats.

---

## Troubleshooting

- **No Android device detected**: tap *Allow* on the USB debugging popup; try another cable.
- **Wrong key**: the backup on the phone must be made *after* you set the 64-digit key. Tap BACK UP NOW and rerun.
- **Backup is encrypted**: untick *Encrypt local backup* in Finder and back up again. Unencrypted backups don't carry saved passwords, Health or Wi-Fi data (those come back from iCloud if you use it).
- **WhatsApp database not in backup**: open WhatsApp on the iPhone once (registered), then back up again.
- **Undo before restoring**: `bash ~/WhatsApp-Migration/iphone_originals/<backup>-<time>/rollback.sh`

---

## How it works

- Android backups (`msgstore.db.crypt15`) are decrypted with [wa-crypt-tools](https://github.com/ElDavoo/wa-crypt-tools) and validated.
- The iPhone's own `ChatStorage.sqlite` (domain `AppDomainGroup-group.net.whatsapp.WhatsApp.shared`) is copied out of the Finder backup, and rows are inserted into its real Core Data schema — the same approach as [watoi](https://github.com/residentsummer/watoi).
- The edited database is written back, `Manifest.db` sizes are updated, and the stale `-wal` is emptied so it can't be replayed over the new data.

```
src/android.py      ADB pull of backup + media (read-only)
src/decrypt.py      crypt12/14/15 decryption, validated
src/convert.py      merge Android chats into iOS ChatStorage.sqlite
src/iphone.py       backup selection, safety copy, write-back, restore guide
test_migration.py   end-to-end check on synthetic data (python3 test_migration.py)
```

## Limitations

- macOS only. Media files are not placed inside WhatsApp on iOS (placeholders + files on Mac).
- Tested on synthetic databases matching current schemas, not on every WhatsApp version; WhatsApp can change its formats at any time.

---

## Contributing

PRs are welcome. Some areas that would make great contributions:

- [ ] Windows support (replace Mac-specific backup path with Windows equivalent)
- [ ] Real media import into iOS (copy files into `Message/Media` and link `ZWAMEDIAITEM`)
- [ ] Encrypted iPhone backup support

---

## Disclaimer

This tool is intended for personal use — migrating your own WhatsApp data between your own devices. Do not use it to access anyone else's WhatsApp data. Respect WhatsApp's Terms of Service.

---

## License

MIT License — see [LICENSE](LICENSE) for details.

---

## Acknowledgements

- [wa-crypt-tools](https://github.com/ElDavoo/wa-crypt-tools) by ElDavoo — WhatsApp crypt15 decryption
- [watoi](https://github.com/residentsummer/watoi) by residentsummer — iOS ChatStorage import approach
