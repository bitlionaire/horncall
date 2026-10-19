---
name: approver
description: Control Horncall (phone approvals of permission requests). Use when the user types /approver or asks to pair the phone, turn phone approvals on/off ("I'm leaving my desk", "I'm back"), check status, send a test, or unpair.
argument-hint: "pair | on [--hours N] | off | status | test | update | name \"label\" | unpair"
---

# Horncall

Run the launcher with Python, passing the user's arguments (default: `status`).
The launcher lives two directories above this skill's base directory:

```
python "<skill base directory>/../../hooks/approver_launcher.py" <args>
```

On Windows, if `python` is not found, try `py -3`.

| Argument | Effect |
|---|---|
| `pair` | first run creates a private venv (~/.horncall/venv), then prints a QR code to scan in the Android app. Add `--new` to rotate the key, `--server URL` for a self-hosted ntfy. |
| `on` | requests go to the phone (user is away). `--hours N` switches off automatically. |
| `off` | normal permission dialog on this PC. |
| `status` | pairing, mode, server reachability. |
| `test` | sends a test request and waits for the phone's answer. |
| `name "label"` | signs orders from the current project folder with a label shown big on the phone (run from the project folder; `--clear` removes it, no text shows it). Each chat also gets an automatic codename like "Grunt Ironjaw". |
| `update` | downloads the newest plugin release from the relay, checks its signature and installs it over this folder (pairing kept). Tell the user to restart Claude Code afterwards. |
| `unpair` | deletes the key. |

Notes:
- After `pair`, show the user the QR code output as-is (in a code block) so it stays scannable, and also the `horncall://` link.
- While mode is `on`, a question waits on the phone for up to 30 minutes; answering it on the PC instead drops the phone card.
- Never print or log the key except as part of the pairing output the user explicitly asked for.
