"""Command line: python -m approver <command>."""

from __future__ import annotations

import argparse
import os
import sys
import time
import urllib.parse

from . import __version__, agents as agent_registry, crypto, phones, protocol
from .config import Config, State, config_path, home_dir
from .link import Link
from .transport import Ntfy

PAIR_SCHEME = "horncall://pair"


def pairing_uri(config: Config) -> str:
    # ':' and '/' left as they are: a smaller QR, and every app version decodes it the same (URLDecoder).
    query = urllib.parse.urlencode({"v": 1, "s": config.server, "k": config.key, "d": config.device_name}, safe=":/")
    return f"{PAIR_SCHEME}?{query}"


def print_qr(uri: str, say=print) -> bool:
    """The pairing QR drawn in the terminal (works over SSH). False when qrcode isn't installed."""
    try:
        import io
        import qrcode
    except ImportError:
        say("(install 'qrcode' to see a QR code here)")
        return False
    qr = qrcode.QRCode(border=1)
    qr.add_data(uri)
    out = io.StringIO()
    qr.print_ascii(out=out, invert=True)
    say(out.getvalue().rstrip("\n"))
    return True


def command() -> str:
    """How the user runs us: `horncall` (pip/pipx) or `approver` (plugin folder, also /horncall:approver)."""
    from . import install
    return "horncall" if install.packaged() else "approver"


def _require_config() -> Config:
    config = Config.load()
    if config is None:
        sys.exit(f"Not paired yet. Run: {command()} pair")
    return config


def cmd_pair(args) -> None:
    existing = Config.load()
    if existing and not args.new:
        config = existing
        print("Already paired — showing the existing code (use --new for a fresh key).")
    else:
        config = Config(key=crypto.new_key())
    if args.server:
        config.server = args.server
    if args.name:
        config.device_name = args.name
    phones.open_pairing(config)  # a new phone may join with its first signed answer
    config.save()

    uri = pairing_uri(config)
    if print_qr(uri):
        try:
            import qrcode
            from qrcode.image.svg import SvgPathImage
            svg = home_dir() / "pair-qr.svg"
            qrcode.make(uri, image_factory=SvgPathImage).save(str(svg))
            print(f"QR also saved to: {svg}")
        except (ImportError, OSError):
            pass
    print("\nScan it in the Horncall app, or paste this code:\n")
    print(uri)
    print("\nKeep it to yourself: it holds this PC's key (whoever has it can read your orders). Don't share screenshots of it.")
    print(f"\nDevice: {config.device_name}   Server: {config.server}")
    print(f"For the next {phones.PAIRING_WINDOW_S // 60} min a new phone joins with its first answer "
          "(answer one order or send a test).")
    from . import autoupdate
    if autoupdate.describe() == "off":
        print(f"\nTip: let new Horncall versions install themselves: {command()} autoupdate on")


def cmd_unpair(args) -> None:
    config_path().unlink(missing_ok=True)
    State().save()
    print("Unpaired. The key has been deleted.")


def cmd_on(args) -> None:
    _require_config()
    until = time.time() + args.hours * 3600 if args.hours else None
    State(mode="on", on_until=until, on_since=time.time()).save()
    suffix = f" for {args.hours:g} h" if args.hours else ""
    print(f"Approver ON{suffix} — permission requests go to your phone.")
    from . import pcdialog
    if not pcdialog.can_show():
        config = Config.load()
        wait = f"{config.noscreen_wait_s} s, then they ask here in the terminal" if config.noscreen_wait_s else \
            "up to the full timeout (phone only)"
        print(f"No screen here (SSH?): Codex, Gemini, Copilot, CodeBuddy and Crush wait for the phone {wait}. "
              f"Change: {command()} noscreen <seconds>|off")


def cmd_off(args) -> None:
    State(mode="off").save()
    print("Approver OFF — Claude Code asks here, as usual.")


def cmd_replies(args) -> None:
    config = _require_config()
    config.send_replies = args.switch == "on"
    config.save()
    if config.send_replies:
        print("Replies ON — when a task ends, the phone shows the first lines of the AI's answer "
              "(plain text, encrypted, at most 240 characters).")
    else:
        print("Replies OFF — the phone only hears that the task is done.")


def cmd_pcdialog(args) -> None:
    config = _require_config()
    config.pc_dialog = args.switch == "on"
    config.save()
    if config.pc_dialog:
        print("PC window ON — Codex, Gemini and Copilot orders also open a small window here; the first answer counts.")
    else:
        print("PC window OFF — while the phone is on duty, Codex, Gemini and Copilot orders go to the phone only.")


def cmd_noscreen(args) -> None:
    config = _require_config()
    value = args.value.lower()
    if value == "off":
        config.noscreen_wait_s = 0
    elif value.isdigit() and int(value) >= 10:
        config.noscreen_wait_s = int(value)
    else:
        print("Give seconds (10 or more), or off.")
        sys.exit(1)
    config.save()
    if config.noscreen_wait_s:
        print(f"Without a screen (SSH): the phone gets {config.noscreen_wait_s} s, then the AI asks in the terminal.")
    else:
        print("Without a screen (SSH): phone only, up to the full timeout.")


def cmd_phones(args) -> None:
    config = _require_config()
    from . import phones
    if args.action == "forget":
        hits = phones.find(config, args.key or "")
        if len(hits) != 1:
            print(f"{'No' if not hits else len(hits)} trusted phone(s) match '{args.key}'. Give more of the key, as listed by: "
                  f"{command()} phones")
            sys.exit(1)
        if len(config.phones) == 1:
            print(f"That is the only trusted phone: answers would stop counting. Pair a new phone first ({command()} pair).")
            sys.exit(1)
        config.phones.remove(hits[0])
        config.save()
        print(f"Forgotten: {crypto.fingerprint(hits[0]['pub'])} — its answers no longer count.")
        return
    if not config.phones:
        print("No phone trusted yet: the first phone that answers with Horncall 0.5.5+ becomes trusted.")
        return
    day = lambda t: time.strftime("%Y-%m-%d %H:%M", time.localtime(t)) if t else "—"  # noqa: E731
    print("Trusted phones (only their signed answers count):")
    for p in config.phones:
        print(f"  {crypto.fingerprint(p['pub'])}   added {day(p.get('added'))}   last answer {day(p.get('last'))}")
    print(f"Remove one (e.g. an old phone or reinstall): {command()} phones forget <start of its key>")


def cmd_status(args) -> None:
    config = Config.load()
    state = State.load()
    print(f"horncall {__version__}")
    print(f"home:   {home_dir()}")
    if config is None:
        print(f"paired: no  (run: {command()} pair)")
        return
    mode = "on" if state.is_on() else "off"
    if state.mode == "on" and (state.paused_until or 0) > time.time():
        mode = f"off duty until {time.strftime('%H:%M', time.localtime(state.paused_until))} (from the PC window; {command()} on resumes now)"
    if mode == "on" and state.on_until:
        mode += f" (until {time.strftime('%H:%M', time.localtime(state.on_until))})"
    print(f"paired: yes  device={config.device_name}")
    print(f"mode:   {mode}")
    print(f"server: {config.server}  reachable={'yes' if Ntfy(config.server).ping() else 'NO'}")
    print(f"timeout: {config.timeout_s}s, on timeout: {config.on_timeout}")
    print(f"replies: {'on — a finished task sends the start of the answer' if config.send_replies else 'off'}"
          f"  (change: {command()} replies on|off)")
    window = "on — Codex/Gemini/Copilot/CodeBuddy/Crush orders also open a window here" if config.pc_dialog else         "off (back on: the phone app's switch, or pcdialog on)"
    print(f"pc window: {window}"
          f"  (change: {command()} pcdialog on|off)")
    from . import pcdialog
    no_screen = f"{config.noscreen_wait_s} s on the phone, then the terminal" if config.noscreen_wait_s else "phone only"
    here = "" if pcdialog.can_show() else "  <- this machine has no screen"
    print(f"no screen: {no_screen}{here}  (change: {command()} noscreen <s>|off)")
    if config.phones:
        keys = ", ".join(crypto.fingerprint(p["pub"]) for p in config.phones)
        print(f"phones: {len(config.phones)} trusted (key {keys}) — only their signed answers count"
              + (f"  (list / forget: {command()} phones)" if len(config.phones) > 1 else ""))
    else:
        print("phones: none signed yet — the first phone with Horncall 0.5.5+ that answers becomes trusted")
    from . import install, setup
    claude = [how for how, has in (("plugin", setup.claude_plugin_installed()), ("settings.json", install.claude_installed()),
                                   ("status line for HP", install.claude_statusline_installed())) if has]
    agents = ([f"claude ({' + '.join(claude)})"] if claude else []) + [
        name for name, (_, _, has) in install.INSTALLERS.items() if has() and name not in ("claude", "claude-statusline")]
    print(f"agents: {', '.join(agents) or 'none — run setup'}")
    from . import compat
    for warning in compat.floor_warnings([a.split(" ")[0] for a in agents]):
        print(f"warning: {warning}")
    if "plugin" in claude:
        from . import update
        direct = install.direct_hooks_python(update.INSTALL_ROOT / "plugin" / "hooks" / "hooks.json")
        if direct is not None and not direct.is_file():
            print(f"warning: Claude's hooks start {direct}, which is gone — run: {command()} setup")
        elif direct is None and not setup.git_bash_found():  # direct hooks need no Git Bash
            print("warning: Claude Code runs the plugin's hooks with Git Bash, which is missing here — install Git for "
                  f"Windows (git-scm.com), or use hooks in settings.json instead: {command()} install claude")
    from . import autoupdate
    auto = autoupdate.describe()
    print(f"auto-update: {auto}" + (f"  (turn on: {command()} autoupdate on)" if auto == "off" else ""))
    latest =(Ntfy(config.server).latest_versions() or {}).get("plugin", {})
    if _newer(latest.get("version", ""), __version__):
        print(f"update: plugin {latest['version']} is available — run: {command()} update")


def _newer(candidate: str, current: str) -> bool:
    """'0.3.0' > '0.2.1'; anything unparsable counts as not newer."""
    try:
        return tuple(map(int, candidate.split("."))) > tuple(map(int, current.split(".")))
    except ValueError:
        return False


def cmd_test(args) -> None:
    config = _require_config()
    link = Link(config.key, config.server)
    request = protocol.build_request(
        {"tool_name": "Test", "tool_input": {"description": "Test from approver CLI",
                                              "message": "Ready for orders?"}, "cwd": None},
        config.device_name, args.timeout, config.max_preview_chars)
    print(f"Sending test request {request['id']} — answer it on the phone (waiting {args.timeout}s)…")
    response = link.request_and_wait(request, time.time() + args.timeout)
    if response is None:
        print("No answer (timeout).")
        sys.exit(1)
    print(f"Answer: {response['decision'].upper()} via {response.get('via')}")


def cmd_sim(args) -> None:
    """Pretend to be the phone: receive requests in this terminal and answer them."""
    config = _require_config()
    link = Link(config.key, config.server)
    print(f"Phone simulator listening on {config.server} … (Ctrl+C to stop)")
    deadline = time.time() + 365 * 24 * 3600
    try:
        for message in link.listen_requests(int(time.time()), deadline):
            if message.get("type") == "cancel":
                print(f"\n[cancelled {message.get('id')}: {message.get('reason')}]")
                continue
            if message.get("type") != "request":
                continue
            danger = " !!! DANGER !!!" if message.get("risk") == "danger" else ""
            print("\n" + "=" * 60)
            who = " · ".join(x for x in (message.get("label"), message.get("session")) if x)
            print(f"«Awaiting orders»  {who + ' — ' if who else ''}{message.get('device')} / {message.get('project')}{danger}")
            print(f"{message.get('title')}")
            if message.get("description"):
                print(f"  ({message['description']})")
            print("-" * 60)
            print(message.get("preview", ""))
            print("-" * 60)
            if args.auto:
                decision = args.auto
            else:
                answer = input("Allow? [y]es / [n]o: ").strip().lower()
                decision = "allow" if answer in ("y", "t", "yes", "tak") else "deny"
            link.send_response(message["id"], decision, "button")
            print("→ «Moving out!»" if decision == "allow" else "→ «Negative.»")
    except KeyboardInterrupt:
        print()


def cmd_name(args) -> None:
    """Label a project folder; every Claude session working there signs its orders with it."""
    config = _require_config()
    key = protocol.path_key(args.path or os.environ.get("HORNCALL_CWD") or os.getcwd())
    if args.clear:
        config.labels.pop(key, None)
        config.save()
        print(f"Label removed for {key}")
    elif args.label:
        config.labels[key] = " ".join(args.label).strip()
        config.save()
        print(f"Orders from {key} will be signed: «{config.labels[key]}»")
    else:
        print(f"{key}: «{config.labels[key]}»" if key in config.labels else f"No label for {key}")
        for other, label in sorted(config.labels.items()):
            if other != key:
                print(f"  {other}: «{label}»")


def cmd_hook(args) -> None:
    from .hook import main
    sys.exit(main(args.agent))


def cmd_posttool(args) -> None:
    from .hook import main_posttool
    sys.exit(main_posttool(args.agent))


def cmd_event(args) -> None:
    from .hook import main_event
    sys.exit(main_event(args.agent, args.state))


def cmd_install(args) -> None:
    from . import install
    if args.print:
        # Show, don't touch: exactly what would be added or replaced, and where.
        for path, part in install.preview(args.agent):
            print(f"# {path}  (merged in; your other settings stay; one-time backup: {path.name}.horncall-backup)")
            print(part)
        return
    if args.agent == "claude":
        from . import setup
        if setup.claude_plugin_installed():
            sys.exit("Claude Code already has the Horncall plugin (it brings these hooks). "
                     "Remove it first (/plugin uninstall horncall@horncall) to use settings.json hooks instead.")
    do, _, _ = install.INSTALLERS[args.agent]
    path = do()
    if args.agent == "claude-statusline":
        print(f"Claude Code status line set in {path} — Claude's plan limits now reach the phone's HP bar.")
        print("It shows from Claude's next reply (a status line you had before keeps showing).")
        return
    print(f"{args.agent}: Horncall hook added to {path}")
    print(f"Restart that tool. Its permission questions go to the phone while `{command()} on` is set.")


def cmd_update(args) -> None:
    from . import update
    from .config import DEFAULT_SERVER
    config = Config.load()
    if args.auto:
        # Started in the background by the launcher (autoupdate.py): only a ZIP install with the switch on, and
        # whatever happens goes to hook.log, never to a terminal.
        from . import autoupdate
        from .hook import log
        if autoupdate.kind() != "zip" or not autoupdate.is_on():
            return
        said: list[str] = []
        try:
            code = update.run(config.server if config else DEFAULT_SERVER, say=said.append)
        except (update.UpdateError, OSError) as exc:
            code, said = 1, [f"failed, nothing installed: {exc}"]
        log("auto-update", code=code, said=" / ".join(said)[:300])
        return
    try:
        sys.exit(update.run(config.server if config else DEFAULT_SERVER))
    except (update.UpdateError, OSError) as exc:
        sys.exit(f"Update failed, nothing installed: {exc}")


def cmd_autoupdate(args) -> None:
    from . import autoupdate
    if args.switch is None:
        print(f"automatic updates: {autoupdate.describe()}")
        return
    print(autoupdate.switch(args.switch == "on"))


def cmd_setup(args) -> None:
    from . import setup
    sys.exit(setup.run())


def cmd_uninstall(args) -> None:
    from . import install
    if args.agent != "all":
        _, undo, _ = install.INSTALLERS[args.agent]
        print(f"{args.agent}: hook removed" if undo() else f"{args.agent}: no Horncall hook found")
        return
    # Everything this installation added: hooks of every AI, the Claude plugin, the program folder.
    from . import setup
    for name, (_, undo, _) in install.INSTALLERS.items():
        print(f"{name}: removed" if undo() else f"{name}: nothing to remove")
    print(setup.uninstall_claude_plugin())
    print(setup.remove_app_dir())
    print(f"Your pairing and settings stay in {home_dir()} — delete that folder too to remove everything "
          "(or run: approver unpair).")


def build_parser(prog: str = "approver") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=prog, description="Horncall")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("pair", help="create a key and show the pairing QR code")
    p.add_argument("--new", action="store_true", help="generate a new key even if already paired")
    p.add_argument("--server", help="ntfy server URL (default: the Horncall relay)")
    p.add_argument("--name", help="device name shown on the phone")
    p.set_defaults(func=cmd_pair)

    sub.add_parser("setup", help="guided setup: hook up the AI tools, pair the phone, send a test")\
        .set_defaults(func=cmd_setup)
    p = sub.add_parser("update", help="install the newest signed plugin release from the relay")
    p.add_argument("--auto", action="store_true", help=argparse.SUPPRESS)  # the launcher's daily background run
    p.set_defaults(func=cmd_update)
    p = sub.add_parser("autoupdate", help="new plugin versions install themselves (on/off; no argument: show)")
    p.add_argument("switch", nargs="?", choices=["on", "off"])
    p.set_defaults(func=cmd_autoupdate)
    sub.add_parser("unpair", help="delete the key").set_defaults(func=cmd_unpair)

    p = sub.add_parser("on", help="forward permission requests to the phone")
    p.add_argument("--hours", type=float, help="switch back off automatically after N hours")
    p.set_defaults(func=cmd_on)

    sub.add_parser("off", help="use the normal dialog on this PC").set_defaults(func=cmd_off)
    sub.add_parser("status", help="show pairing and mode").set_defaults(func=cmd_status)
    p = sub.add_parser("replies", help="send the start of the AI's answer with 'mission complete' (off by default)")
    p.add_argument("switch", choices=["on", "off"])
    p.set_defaults(func=cmd_replies)
    p = sub.add_parser("pcdialog", help="Codex/Gemini/Copilot orders also open a window on this PC (on by default)")
    p.add_argument("switch", choices=["on", "off"])
    p.set_defaults(func=cmd_pcdialog)
    p = sub.add_parser("noscreen", help="where no PC window can open (SSH): seconds for the phone before the terminal asks")
    p.add_argument("value", help="seconds (default 120), or off = phone only")
    p.set_defaults(func=cmd_noscreen)
    p = sub.add_parser("phones", help="list trusted phones, or forget one (old phone, reinstalled app)")
    p.add_argument("action", nargs="?", choices=["list", "forget"], default="list")
    p.add_argument("key", nargs="?", help="start of the key fingerprint to forget, e.g. EA:85")
    p.set_defaults(func=cmd_phones)

    p = sub.add_parser("test", help="send a test request to the phone")
    p.add_argument("--timeout", type=int, default=60)
    p.set_defaults(func=cmd_test)

    p = sub.add_parser("sim", help="phone simulator in the terminal")
    p.add_argument("--auto", choices=["allow", "deny"], help="answer automatically")
    p.set_defaults(func=cmd_sim)

    p = sub.add_parser("name", help="sign orders from a project folder with a label")
    p.add_argument("label", nargs="*", help="label text; empty = show current label")
    p.add_argument("--clear", action="store_true", help="remove the label")
    p.add_argument("--path", help="project folder (default: current directory)")
    p.set_defaults(func=cmd_name)

    p = sub.add_parser("hook", help="permission hook entry point (called by the AI agent)")
    p.add_argument("agent", nargs="?", default="claude", choices=list(agent_registry.AGENTS))
    p.set_defaults(func=cmd_hook)

    p = sub.add_parser("event", help="task start/end hook entry point (called by the AI agent)")
    p.add_argument("agent", choices=list(agent_registry.AGENTS))
    p.add_argument("state", choices=["working", "idle"])
    p.set_defaults(func=cmd_event)

    p = sub.add_parser("posttool", help="after-tool hook: drop the phone card of a question answered on the PC")
    p.add_argument("agent", nargs="?", default="claude", choices=list(agent_registry.AGENTS))
    p.set_defaults(func=cmd_posttool)

    agents = list(agent_registry.AGENTS) + ["claude-statusline"]
    p = sub.add_parser("install", help="connect an AI agent to the phone")
    p.add_argument("agent", choices=agents, help="claude: hooks in Claude's settings.json (instead of the plugin); "
                                                 "claude-statusline: Claude's plan limits for the phone's HP bar")
    p.add_argument("--print", action="store_true", help="only show what would be written, change nothing")
    p.set_defaults(func=cmd_install)
    p = sub.add_parser("uninstall", help="disconnect an AI agent, or 'all' to remove everything Horncall added")
    p.add_argument("agent", choices=agents + ["all"])
    p.set_defaults(func=cmd_uninstall)
    return parser


def main(argv: list[str] | None = None, prog: str = "approver") -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    args = build_parser(prog).parse_args(argv)
    args.func(args)
