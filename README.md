# Horncall

**One app to approve them all.** Answer your AI coding agents' permission requests from your Android
phone — Claude Code, Gemini CLI, GitHub Copilot CLI and OpenAI Codex CLI, on any number of computers.

This repository is the **PC add-on** (open source, MIT). It hooks into the agents' own, documented
hook systems and forwards a permission question to the phone only while you switch it **on**
(you're away from the desk). At the desk, everything works as before.

- Guide: **https://horncall.bitlionaire.me** · phone app: Android (see the guide)
- End-to-end encrypted: AES-256-GCM with a key made on your computer and handed to the phone only by
  QR code. The relay sees ciphertext.
- The phone **signs every decision** (Android Keystore, ECDSA P-256); the computer accepts only
  answers signed by the phone it paired with. A stolen key file can read orders, but approve nothing.
- Dangerous commands (`rm -rf`, force push, `DROP TABLE`, edits to secrets, hooks or CI…) get a red
  card with the reasons and need a long press.

## Install

Pick one. All of them end with the same pairing step (`setup` shows a QR code to scan in the app).

**Claude Code plugin** (Claude Code keeps it up to date):

```
/plugin marketplace add bitlionaire/horncall
/plugin install horncall@horncall
/horncall:approver setup
```

**pipx / uv** (a `horncall` command; hooks for every agent, Claude Code via its `settings.json`):

```
pipx install horncall        # or: uv tool install horncall
horncall setup
```

**ZIP with a wizard** (no Python tooling needed beyond Python 3.10+): download `horncall-plugin.zip`
from the guide, unpack anywhere and run `setup.cmd` (macOS/Linux: `sh setup.sh`). It copies itself to
the per-user program folder (`%LOCALAPPDATA%\Horncall\app`, `~/Library/Application Support/Horncall/app`,
`~/.local/share/horncall/app`); the unpacked folder can be deleted.

**By hand**: see exactly what would be added, then paste it yourself or let it write:

```
horncall install gemini --print     # claude | gemini | copilot | codex | claude-statusline
horncall install gemini
```

## What the install changes

| Where | What |
|---|---|
| `~/.horncall` | pairing key, trusted phones, on/off mode, log (plugin/ZIP installs also a private `venv`) |
| Claude Code | the `horncall` plugin, or (pipx) four hooks in `~/.claude/settings.json`; optionally a status line for plan limits |
| `~/.gemini/settings.json`, `~/.codex/hooks.json`, `~/.copilot/hooks/horncall.json` | Horncall hook entries, only for the agents you pick |

Other settings stay untouched; the first change to a file leaves a `*.horncall-backup` copy. The setup
wizard lists the files and asks before writing. No services, autostart entries or admin rights.
Remove everything: `horncall uninstall all` (in Claude: `/horncall:approver uninstall all`).

## Every day

| Command | |
|---|---|
| `horncall on` / `/horncall:approver on` | leaving the desk — questions go to the phone (`--hours 2` switches off by itself) |
| `horncall off` | back — normal questions on the computer |
| `horncall status` | pairing, mode, server, trusted phones, connected agents |
| `horncall name "Shop backend"` | label this project's orders on the phone |

Gemini CLI asks on its own first — start it with `gemini --yolo` (same as `--approval-mode yolo`) so only the phone asks.
Codex CLI: open `/hooks` and enable the three Horncall hooks.

## How it works

The agent runs a hook (`horncall hook <agent>`) on a permission request. While Horncall is off it
exits at once and the agent shows its normal dialog. While on, it encrypts the request, publishes it
to an [ntfy](https://ntfy.sh) relay (ours by default, any ntfy server works: `horncall pair --server`)
and waits for the phone's signed answer, which it returns to the agent as allow or deny. Answering
on the computer instead removes the card from the phone.

## Development

```
python -m pip install -r plugin/requirements.txt pytest
python -m pytest
```

Issues and pull requests are welcome. Horncall is an independent project, not affiliated with
Anthropic, OpenAI, Google or GitHub; their product names only describe compatibility.
Contact: bitlionaire.me@gmail.com
