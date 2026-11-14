---
name: approver
description: Control Horncall (phone approvals of permission requests). Use when the user types /approver or asks to pair the phone, turn phone approvals on/off ("I'm leaving my desk", "I'm back"), check status, send a test, or unpair.
argument-hint: "on [--hours N] | off [--hours N] | status | pair | test | help"
context: fork
model: haiku
background: false
---

Run exactly one command, with the user's arguments (none given: `status`). The launcher is two directories above
this skill's base directory:

```
python "<skill base directory>/../../hooks/approver_launcher.py" <args>
```

On Windows, if `python` is not found, use `py -3` instead of `python`.

- `pair` → run it as `pair --open` (add any other arguments the user gave). It opens the QR on the screen; never
  print a QR code, a `horncall://` link or a key.
- Everyday: `on [--hours N]` (requests go to the phone), `off` (the PC asks as usual), `off --hours N` (this PC
  sends nothing for N hours), `status`, `test`. Everything else: `help`.
- After `update`, tell the user to restart Claude Code.

Your final message is shown to the user: the command's output, unchanged and without comments.
