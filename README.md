# Horncall

**Supports Claude Code, OpenAI Codex CLI, Gemini CLI, GitHub Copilot CLI, CodeBuddy Code, Crush and opencode**
(new in 0.7: CodeBuddy, Crush, opencode).

**One app to approve them all.** Answer your AI coding agents' permission requests from your Android
phone, on any number of computers.

This repository is the **PC add-on** (open source, MIT). It hooks into the agents' own, documented
hook systems and forwards a permission question to the phone only while you switch it **on**
(you're away from the desk). At the desk, everything works as before.

- Guide: **https://horncall.bitlionaire.me** · phone app: [Google Play](https://play.google.com/store/apps/details?id=app.horncall) (Android)
- End-to-end encrypted: AES-256-GCM with a key made on your computer and handed to the phone only by
  QR code. The relay sees ciphertext.
- The phone **signs every decision** (Android Keystore, ECDSA P-256); the computer accepts only
  answers signed by the phone it paired with. A stolen key file can read orders, but approve nothing.
- Dangerous commands (`rm -rf`, force push, `DROP TABLE`, edits to secrets, hooks or CI…) get a red
  card with the reasons and need a long press.

## Supported versions

Keep the agents up to date. Horncall supports these versions and newer; older ones are not worked around
(`horncall setup` and `horncall status` tell you to update). The list lives in
[`plugin/approver/compat.json`](plugin/approver/compat.json) (`support.floor`).

| Agent | Minimum |
|---|---|
| Claude Code | 2.1.139 |
| Gemini CLI | 0.27.0 |
| GitHub Copilot CLI | 1.0.16 |
| OpenAI Codex CLI | 0.163.0 |
| CodeBuddy Code | 2.97.0 |
| Crush | 0.99.0 |
| opencode (`@opencode/cli`, not the old `opencode-ai`) | 2.2.2 |

## Install

Pick one. All of them end with the same pairing step: `setup` shows a QR code to scan in the app, in the
browser or, over SSH and on headless servers, right in the terminal (`horncall pair` draws it there too).

**Claude Code plugin**:

```
/plugin marketplace add bitlionaire/horncall
/plugin install horncall@horncall
/horncall:approver setup
/horncall:approver autoupdate on
```

Claude Code updates plugins from other marketplaces only when you turn it on: `autoupdate on` does that for
Horncall (or `/plugin` → Marketplaces → horncall → Enable auto-update). Needs Python 3.10+ (on Windows the
`py` launcher from python.org is enough) and, on Windows, Git for Windows (Claude Code runs plugin hooks with Git Bash).

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
horncall install gemini --print     # claude | gemini | copilot | codex | codebuddy | crush | opencode | claude-statusline
horncall install gemini
```

## What the install changes

| Where | What |
|---|---|
| `~/.horncall` | pairing key, trusted phones, on/off mode, log (plugin/ZIP installs also a private `venv`) |
| Claude Code | the `horncall` plugin, or (pipx) four hooks in `~/.claude/settings.json`; optionally a status line for plan limits |
| `~/.gemini/settings.json`, `~/.codex/hooks.json`, `~/.copilot/hooks/horncall.json`, `~/.codebuddy/settings.json`, `~/.config/crush/crushrc` | Horncall hook entries, only for the agents you pick |
| `~/.config/opencode/plugins/horncall.ts` | a small opencode plugin that passes its questions to the phone |

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
| `horncall replies on` | optional task report: the start of the AI's answer on the phone when a task ends (encrypted, off by default) |
| `horncall pcdialog off` | no small Horncall window on the PC for Codex, Gemini, Copilot, CodeBuddy and Crush (they show nothing of their own while the phone is asked; with the window on, the first answer counts) |
| `horncall phones` / `phones forget <key>` | list trusted phones / remove one |
| `horncall noscreen 300` / `noscreen off` | without a screen (SSH): how long the phone is asked before the AI asks in the terminal (default 120 s) / phone only |

**The PC window.** While the phone is on duty, Codex, Gemini, Copilot, CodeBuddy and Crush also get a small Horncall window on the
PC (Claude Code and opencode ask on their own, next to the phone). It can pop up while you work on something else and one of these
AIs runs in the background. Only a click approves (Enter does nothing); below the buttons, "Off duty for 2 h" (this PC stops asking the phone; duty resumes by itself) and "Turn PC windows off" (back on with the switch in the app, or `horncall pcdialog on`).
At your desk, take the phone off duty (`horncall off` or "Duty" in the app) or turn the window off (`horncall pcdialog off`).

Gemini CLI asks on its own first — start it with `gemini --yolo` (same as `--approval-mode yolo`) so only the phone asks.
Codex CLI: open `/hooks` and enable the five Horncall hooks; its own questions (`request_user_input`) show on the phone as a
"Question" card, answered on the PC.
CodeBuddy Code: restart it and approve the Horncall hooks in its `/hooks` panel. Crush: restart it
(Windows: `%USERPROFILE%\.config\crush\crushrc`).
opencode: restart it. Its own question stays open and the phone can answer it too (first answer counts). opencode only asks
about what its permission rules say `ask`; Horncall mirrors those questions and doesn't change the rules. For example, in
`opencode.jsonc`:

```
{"permissions": [{"action": "shell", "resource": "*", "effect": "ask"},
                 {"action": "edit",  "resource": "*", "effect": "ask"}]}
```

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
Anthropic, OpenAI, Google, GitHub, Tencent, Charm or the opencode project; their product names only describe compatibility.
Contact: bitlionaire.me@gmail.com
