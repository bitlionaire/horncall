"""Encrypted link between the PC and the phone on top of a transport."""

from __future__ import annotations

import time
from typing import Callable, Iterator

from . import crypto, protocol
from .transport import Ntfy


class Link:
    def __init__(self, key: str, server: str, transport: Ntfy | None = None):
        self.key = key
        self.transport = transport or Ntfy(server)
        self.topic_req = crypto.derive_topic(key, "req")
        self.topic_res = crypto.derive_topic(key, "res")

    # PC side ---------------------------------------------------------------
    def send_request(self, request: dict) -> None:
        body = protocol.fit_to_budget(request, lambda m: crypto.seal(self.key, self.topic_req, m))
        self.transport.publish(self.topic_req, body)

    def send_status(self, status: dict) -> None:
        self.transport.publish(self.topic_req, crypto.seal(self.key, self.topic_req, status), priority=3)

    def send_resolved(self, fp: str, request_id: str | None = None) -> None:
        message = protocol.make_resolved(fp, request_id)
        self.transport.publish(self.topic_req, crypto.seal(self.key, self.topic_req, message), priority=3)

    def send_cancel(self, request_id: str, reason: str) -> None:
        self.transport.publish(self.topic_req, crypto.seal(
            self.key, self.topic_req, protocol.make_cancel(request_id, reason)))

    def request_and_wait(self, request: dict, deadline: float,
                         accept: Callable[[dict], bool] | None = None) -> dict | None:
        """Subscribe to responses first, send the request once subscribed, wait for the answer.

        `accept` can reject an answer (e.g. not signed by a trusted phone); waiting then goes on.
        """
        since = int(time.time()) - 5
        on_open = lambda: self.send_request(request)  # noqa: E731
        for message in self._messages(self.topic_res, since, deadline, on_open):
            if message.get("type") == "response" and message.get("id") == request["id"] \
                    and message.get("decision") in ("allow", "deny") and (accept is None or accept(message)):
                return message
        return None

    # Phone side (used by the simulator; the Android app implements the same) ---
    def listen_requests(self, since: int, deadline: float) -> Iterator[dict]:
        yield from self._messages(self.topic_req, since, deadline)

    def send_response(self, request_id: str, decision: str, via: str, signer=None) -> None:
        """signer: optional (public_key_b64u, sign(bytes) -> signature_b64u), like the app's device key."""
        response = protocol.make_response(request_id, decision, via)
        if signer is not None:
            public_key, sign = signer
            response["pub"], response["sig"] = public_key, sign(protocol.signed_payload(response))
        self.transport.publish(self.topic_res, crypto.seal(self.key, self.topic_res, response))

    # -----------------------------------------------------------------------
    def _messages(self, topic: str, since: int, deadline: float, on_open=None) -> Iterator[dict]:
        seen: set[str] = set()
        for body in self.transport.subscribe(topic, since, deadline, on_open):
            try:
                message = crypto.open_envelope(self.key, topic, body)
            except crypto.CryptoError:
                continue  # noise or forgery on the topic
            key = f"{message.get('type')}:{message.get('id')}"
            if key in seen or not protocol.is_fresh(message, time.time()):
                continue
            seen.add(key)
            yield message
