"""Command line: python -m approver <command>."""

from __future__ import annotations

import argparse
import os
import sys
import time
import urllib.parse

from . import __version__, crypto, phones, protocol
from .config import Config, State, config_path, home_dir
from .link import Link
from .transport import Ntfy

PAIR_SCHEME = "horncall://pair"


def pairing_uri(config: Config) -> str:
    query = urllib.parse.urlencode({"v": 1, "s": config.server, "k": config.key, "d": config.device_name})
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
    print(f"\nDevice: {config.device_name}   Server: {config.server}")
    print(f"For the next {phones.PAIRING_WINDOW_S // 60} min a new phone joins with its first answer "
          "(answer one order or send a test).")


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


def cmd_off(args) -> None:
    State(mode="off").save()
    print("Approver OFF — Claude Code asks here, as usual.")


def cmd_status(args) -> None:
    config = Config.load()
    state = State.load()
    print(f"horncall {__version__}")
    print(f"home:   {home_dir()}")
    if config is None:
        print(f"paired: no  (run: {command()} pair)")
        return
    mode = "on" if state.is_on() else "off"
    if mode == "on" and state.on_until:
        mode += f" (until {time.strftime('%H:%M', time.localtime(state.on_until))})"
    print(f"paired: yes  device={config.device_name}")
    print(f"mode:   {mode}")
    print(f"server: {config.server}  reachable={'yes' if Ntfy(config.server).ping() else 'NO'}")
    print(f"timeout: {config.timeout_s}s, on timeout: {config.on_timeout}")
    if config.phones:
        keys = ", ".join(crypto.fingerprint(p["pub"]) for p in config.phones)
        print(f"phones: {len(config.phones)} trusted (key {keys}) — only their signed answers count")
    else:
        print("phones: none signed yet — the first phone with Horncall 0.5.5+ that answers becomes trusted")
    from . import install, setup
    claude = [how for how, has in (("plugin", setup.claude_plugin_installed()), ("settings.json", install.claude_installed()),
                                   ("status line for HP", install.claude_statusline_installed())) if has]
    agents = ([f"claude ({' + '.join(claude)})"] if claude else []) + [
        name for name, (_, _, has) in install.INSTALLERS.items() if has() and name not in ("claude", "claude-statusline")]
    print(f"agents: {', '.join(agents) or 'none — run setup'}")
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
    try:
        sys.exit(update.run(config.server if config else DEFAULT_SERVER))
    except (update.UpdateError, OSError) as exc:
        sys.exit(f"Update failed, nothing installed: {exc}")


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
    sub.add_parser("update", help="install the newest signed plugin release from the relay")\
        .set_defaults(func=cmd_update)
    sub.add_parser("unpair", help="delete the key").set_defaults(func=cmd_unpair)

    p = sub.add_parser("on", help="forward permission requests to the phone")
    p.add_argument("--hours", type=float, help="switch back off automatically after N hours")
    p.set_defaults(func=cmd_on)

    sub.add_parser("off", help="use the normal dialog on this PC").set_defaults(func=cmd_off)
    sub.add_parser("status", help="show pairing and mode").set_defaults(func=cmd_status)

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
    p.add_argument("agent", nargs="?", default="claude", choices=["claude", "gemini", "copilot", "codex"])
    p.set_defaults(func=cmd_hook)

    p = sub.add_parser("event", help="task start/end hook entry point (called by the AI agent)")
    p.add_argument("agent", choices=["claude", "gemini", "copilot", "codex"])
    p.add_argument("state", choices=["working", "idle"])
    p.set_defaults(func=cmd_event)

    p = sub.add_parser("posttool", help="after-tool hook: drop the phone card of a question answered on the PC")
    p.add_argument("agent", nargs="?", default="claude", choices=["claude", "gemini", "copilot", "codex"])
    p.set_defaults(func=cmd_posttool)

    agents = ["claude", "gemini", "copilot", "codex", "claude-statusline"]
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
