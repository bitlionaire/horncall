"""ntfy transport: publish a message body to a topic, stream message bodies from a topic."""

from __future__ import annotations

import json
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Iterator

from . import __version__

USER_AGENT = f"horncall/{__version__}"
KEEPALIVE_SLACK_S = 60  # ntfy sends keepalives every ~45 s


DEBUG = bool(os.environ.get("HORNCALL_DEBUG") or os.environ.get("ZUGZUG_DEBUG"))


def _debug(*parts) -> None:
    if DEBUG:
        print(f"[ntfy {time.strftime('%H:%M:%S')}]", *parts, file=sys.stderr, flush=True)


class TransportError(Exception):
    pass


class Ntfy:
    def __init__(self, server: str):
        self.server = server.rstrip("/")

    def publish(self, topic: str, body: str, timeout: float = 15, priority: int = 4) -> None:
        req = urllib.request.Request(
            f"{self.server}/{topic}", data=body.encode("utf-8"), method="POST",
            # Priority 4 ("high"): the relay forwards to Firebase, which must wake a sleeping phone.
            # Status updates use 3: they can wait until the phone wakes anyway.
            headers={"User-Agent": USER_AGENT, "Content-Type": "text/plain; charset=utf-8", "X-Priority": str(priority)})
        _debug("publish", topic, len(body), "B")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                resp.read()
        except (urllib.error.URLError, OSError) as exc:
            raise TransportError(f"publish failed: {exc}") from exc

    def subscribe(self, topic: str, since: int | str, deadline: float,
                  on_open: Callable[[], None] | None = None) -> Iterator[str]:
        """Yield message bodies from `topic` until `deadline` (unix time). Reconnects on errors.

        `on_open` runs once, when the server confirms the first subscription. Publish anything you
        expect a reply to from there: ntfy.sh writes its cache in batches, so a reply sent before
        the subscription is live may be missed even with `since`.
        """
        cursor: int | str = since
        backoff = 1.0
        while time.time() < deadline:
            query = urllib.parse.urlencode({"since": cursor})
            req = urllib.request.Request(f"{self.server}/{topic}/json?{query}",
                                         headers={"User-Agent": USER_AGENT})
            remaining = deadline - time.time()
            _debug("subscribe", topic, "since", cursor)
            try:
                with urllib.request.urlopen(req, timeout=min(remaining, KEEPALIVE_SLACK_S) + 1) as resp:
                    backoff = 1.0
                    while time.time() < deadline:
                        line = resp.readline()
                        if not line:
                            break  # server closed the stream; reconnect
                        try:
                            event = json.loads(line)
                        except ValueError:
                            continue
                        _debug("event", event.get("event"), topic)
                        if event.get("event") == "open" and on_open is not None:
                            callback, on_open = on_open, None
                            callback()
                        elif event.get("event") == "message":
                            cursor = event.get("id", cursor)
                            yield event.get("message", "")
            except socket.timeout:
                _debug("read timeout, reconnecting")
                continue
            except (urllib.error.URLError, OSError) as exc:
                _debug("error", exc)
                time.sleep(min(backoff, max(0.0, deadline - time.time())))
                backoff = min(backoff * 2, 15)

    def latest_versions(self, timeout: float = 5) -> dict | None:
        """The relay's /app/version.json (published by tools/publish_update.py), or None."""
        try:
            req = urllib.request.Request(f"{self.server}/app/version.json", headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except (urllib.error.URLError, OSError, ValueError):
            return None

    def ping(self, timeout: float = 5) -> bool:
        try:
            req = urllib.request.Request(f"{self.server}/v1/health", headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read()).get("healthy", False) is True
        except (urllib.error.URLError, OSError, ValueError):
            return False
