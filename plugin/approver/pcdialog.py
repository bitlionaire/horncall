"""A small window on the PC with the same order the phone shows, for agents that hide their own prompt while
our hook waits (Codex, Gemini, Copilot): Claude Code shows its dialog next to the hook, these show nothing
but "Running hook". The first answer wins, the phone's or this window's.

The hook starts the window as its own process (``python pcdialog.py <order file>``) and keeps waiting for the
phone. A click writes the answer next to the order file; the hook sees it, answers the agent and takes the card
off the phone. When the hook is done it deletes the order file and the window closes by itself.

Run as a script this file needs nothing but the standard library (tkinter): it must open fast and must not
depend on the add-on's packages. No screen (SSH, Linux without a display) or no tkinter = no window, no error.
"""

from __future__ import annotations

import json
import locale
import os
import subprocess
import sys
from pathlib import Path

ANSWERS = ("allow", "deny", "offduty", "off")  # offduty: this PC off duty 2 h; off: no PC windows (phone still waits)

TEXTS = {
    "en": {"allow": "Allow", "deny": "Deny", "danger": "DANGEROUS", "title": "Horncall: {agent} asks",
           "hint": "{agent} is waiting for a decision. You can answer here or on your phone.",
           "offduty": "Off duty for 2 h (the AI asks here, the phone rests)", "off": "Turn PC windows off"},
    "pl": {"allow": "Zezwól", "deny": "Odmów", "danger": "GROŹNE", "title": "Horncall: {agent} pyta",
           "hint": "{agent} czeka na decyzję. Możesz odpowiedzieć tutaj albo na telefonie.",
           "offduty": "Zejdź ze służby na 2 h (AI pyta tutaj, telefon odpoczywa)", "off": "Wyłącz okienka na komputerze"},
}


# The phone app's look (android ui/skin/Skin.kt "Commander" colours and the factions' logo colours).
GROUND, PANEL, BORDER, INSET = "#0E1013", "#171A1F", "#2A2F37", "#0B0D10"
TEXT, MUTED, APPROVE, DENY = "#E8E6E1", "#9AA0A8", "#46A758", "#E5484D"
FACTION_COLORS = {"claude": "#D97757", "gemini": "#3186FF", "copilot": "#A371F7", "codex": "#ECEEF1",
                  "codebuddy": "#32E6B9", "crush": "#F060F0", "opencode": "#7FDD3C"}


def can_show() -> bool:
    if os.environ.get("HORNCALL_NO_PC_DIALOG") == "1":
        return False
    if sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return False
    return not os.environ.get("SSH_CONNECTION")  # the window would open on the far machine's desk, if anywhere


class Dialog:
    """The hook's handle on the window: read the click, close it."""

    def __init__(self, order_file: Path, process: subprocess.Popen | None):
        self.order_file = order_file
        self.answer_file = order_file.with_suffix(".answer")
        self.process = process

    def answer(self) -> str | None:
        try:
            value = self.answer_file.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return value if value in ANSWERS else None

    def close(self) -> None:
        for path in (self.order_file, self.answer_file):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        if self.process is not None and self.process.poll() is None:
            try:
                self.process.terminate()
            except OSError:
                pass


def start(request: dict, agent_title: str, folder: Path) -> Dialog | None:
    """Open the window for this order; None if there is no screen or it can't start (the phone still works)."""
    if not can_show():
        return None
    order = {"agent": agent_title, "color": FACTION_COLORS.get(str(request.get("agent")), MUTED),
             "unit": request.get("label") or request.get("session") or "",
             "project": request.get("project") or "", "title": request.get("title") or "",
             "preview": request.get("preview") or "", "reasons": request.get("reasons") or [],
             "danger": request.get("risk") == "danger"}
    try:
        folder.mkdir(parents=True, exist_ok=True)
        order_file = folder / f"{request['fp']}.{request['id']}.pc"
        order_file.write_text(json.dumps(order, ensure_ascii=False), encoding="utf-8")
        python = Path(sys.executable)
        windowless = python.with_name("pythonw.exe")  # Windows: no console window behind ours
        exe = windowless if os.name == "nt" and windowless.exists() else python
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        process = subprocess.Popen([str(exe), str(Path(__file__)), str(order_file)], stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
    except (OSError, KeyError):
        return None
    return Dialog(order_file, process)


# --- the window process -----------------------------------------------------------------------------

def _language() -> str:
    try:
        if os.name == "nt":
            import ctypes
            lang_id = ctypes.windll.kernel32.GetUserDefaultUILanguage()
            return "pl" if lang_id & 0x3FF == 0x15 else "en"  # LANG_POLISH
        code = (locale.getlocale()[0] or os.environ.get("LANG") or "").lower()
        return "pl" if code.startswith("pl") else "en"
    except Exception:
        return "en"


def _dark_title_bar(root) -> None:
    """Windows 10/11: a dark title bar to match (DWMWA_USE_IMMERSIVE_DARK_MODE); elsewhere nothing."""
    if os.name != "nt":
        return
    try:
        import ctypes
        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        on = ctypes.c_int(1)
        for attribute in (20, 19):  # 20 on current builds, 19 on early Windows 10 ones
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(on), ctypes.sizeof(on)) == 0:
                break
    except Exception:
        pass


def _window(order_file: Path) -> None:
    import tkinter as tk
    import tkinter.font as tkfont

    order = json.loads(order_file.read_text(encoding="utf-8"))
    t = TEXTS[_language()]
    answer_file = order_file.with_suffix(".answer")
    faction = order.get("color") or MUTED

    root = tk.Tk()
    root.title(t["title"].format(agent=order["agent"]))
    root.attributes("-topmost", True)
    root.configure(bg=GROUND)
    root.minsize(560, 0)
    families = set(tkfont.families())
    body = next((f for f in ("Segoe UI", "SF Pro Text", "Ubuntu", "Cantarell") if f in families), "TkDefaultFont")
    mono = next((f for f in ("Cascadia Mono", "Consolas", "Menlo", "DejaVu Sans Mono") if f in families), "TkFixedFont")
    _dark_title_bar(root)

    def choose(value: str) -> None:
        try:
            answer_file.write_text(value, encoding="utf-8")
        finally:
            root.destroy()

    tk.Frame(root, bg=DENY if order["danger"] else faction, height=4).pack(fill="x")  # the faction's (or danger's) stripe
    card = tk.Frame(root, bg=PANEL, padx=18, pady=14)
    card.pack(fill="both", expand=True)

    head = tk.Frame(card, bg=PANEL)
    head.pack(fill="x")
    tk.Label(head, text=f" {order['agent']} ", bg=PANEL, fg=faction, font=(body, 9, "bold"),
             highlightthickness=1, highlightbackground=faction).pack(side="left")
    who = " · ".join(x for x in (order["unit"], order["project"]) if x)
    tk.Label(head, text=f"  {who}", bg=PANEL, fg=MUTED, font=(body, 9)).pack(side="left")

    tk.Label(card, text=order["title"], bg=PANEL, fg=TEXT, font=(body, 13, "bold"), anchor="w", justify="left",
             wraplength=640).pack(fill="x", pady=(10, 6))
    if order["danger"]:
        reasons = "\n".join(f"• {r}" for r in order["reasons"])
        tk.Label(card, text=f"⚠ {t['danger']}\n{reasons}".strip(), bg=PANEL, fg=DENY, font=(body, 10, "bold"),
                 anchor="w", justify="left").pack(fill="x", pady=(0, 6))
    text = tk.Text(card, width=80, height=min(16, max(3, order["preview"].count("\n") + 2)), wrap="word",
                   bg=INSET, fg=TEXT, insertbackground=TEXT, font=(mono, 10), relief="flat", padx=10, pady=8,
                   highlightthickness=1, highlightbackground=BORDER, highlightcolor=BORDER)
    text.insert("1.0", order["preview"])
    text.configure(state="disabled")
    text.pack(fill="both", expand=True)
    tk.Label(card, text=t["hint"].format(agent=order["agent"]), bg=PANEL, fg=MUTED, font=(body, 9), anchor="w",
             justify="left", wraplength=640).pack(fill="x", pady=(8, 0))

    def button(parent, label: str, value: str, bg: str, fg: str, hover: str, bold: bool = False):
        b = tk.Label(parent, text=label, bg=bg, fg=fg, font=(body, 10, "bold" if bold else "normal"),
                     padx=18, pady=8, cursor="hand2")
        b.bind("<Button-1>", lambda _e: choose(value))
        b.bind("<Enter>", lambda _e: b.configure(bg=hover))
        b.bind("<Leave>", lambda _e: b.configure(bg=bg))
        return b

    buttons = tk.Frame(root, bg=GROUND, padx=18, pady=12)
    buttons.pack(fill="x")
    # Deny left, Allow right: the same sides as on the phone's card.
    button(buttons, t["deny"], "deny", "#2A1214", DENY, "#3A1719").pack(side="left")

    button(buttons, f"✓ {t['allow']}", "allow", APPROVE, "#08130B", "#55BA68", bold=True).pack(side="right")
    # Neither link approves anything: off duty hands the order back to the AI's own question, "off" leaves it on the phone.
    quiet = tk.Frame(root, bg=GROUND, padx=18)
    quiet.pack(fill="x", pady=(0, 12))
    for key in ("offduty", "off"):
        link = tk.Label(quiet, text=t[key], bg=GROUND, fg=TEXT, font=(body, 9, "underline"), cursor="hand2")
        link.bind("<Button-1>", lambda _e, k=key: choose(k))
        link.pack(side="left", padx=(0, 16))
    # No keyboard shortcut for allow and no stolen focus: the window pops up while you may be typing elsewhere,
    # and an Enter meant for the terminal must not approve a command (Codex review, 2026-11-05). Only a click counts.
    root.bind("<Escape>", lambda _e: root.destroy())  # just hides it: the phone still waits

    def watch() -> None:
        if not order_file.exists():  # the hook is done (phone answered, timeout, answered in the agent)
            root.destroy()
            return
        root.after(700, watch)

    root.after(700, watch)
    root.lift()
    root.mainloop()


if __name__ == "__main__":
    try:
        _window(Path(sys.argv[1]))
    except Exception:
        pass  # no screen, no tkinter, order already gone: the phone still works
