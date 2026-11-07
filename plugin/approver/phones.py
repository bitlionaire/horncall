"""Which phones may answer: their decisions must be signed by a key the PC trusts (SPEC section 6.3).

A phone is trusted when it first answers with a signature while no phone is known yet (an upgrade
from unsigned versions, or a fresh pairing), or while `approver pair` keeps the door open. From then
on unsigned or foreign answers are ignored, so whoever copies config.json still cannot approve.
"""

from __future__ import annotations

import time

from . import crypto, protocol
from .config import Config

PAIRING_WINDOW_S = 600
LAST_USE_EVERY_S = 3600


def judge(config: Config, response: dict, now: float | None = None) -> str:
    """"ok", "joined" (new phone trusted and saved), or why the answer is ignored."""
    now = now or time.time()
    pub, sig = response.get("pub"), response.get("sig")
    if not pub or not sig:
        # Apps before 0.5.5 don't sign; on pairings made before then they keep working until a
        # signing phone has joined. Pairings made since (signed_only) never accept unsigned answers.
        return "ok" if not config.phones and not config.signed_only else "unsigned"
    if not crypto.verify_signature(str(pub), str(sig), protocol.signed_payload(response)):
        return "bad-signature"
    if any(p.get("pub") == pub for p in config.phones):
        fresh = Config.load()
        if fresh is not None and not any(p.get("pub") == pub for p in fresh.phones):
            return "forgotten"  # `phones forget` ran while this hook was waiting
        _note_use(str(pub), now)
        return "ok"
    if not config.phones or (config.pairing_until or 0) > now:
        config.phones.append({"pub": pub, "added": int(now)})
        config.save()
        return "joined"
    return "unknown-phone"


def _note_use(pub: str, now: float) -> None:
    """Remember when each phone last answered (shown by `approver phones`), at most once an hour.
    Reloads the config first: the hook's copy is minutes old and the user may have changed a setting since."""
    try:
        fresh = Config.load()
        phone = next((p for p in fresh.phones if p.get("pub") == pub), None) if fresh else None
        if phone is not None and now - phone.get("last", 0) >= LAST_USE_EVERY_S:
            phone["last"] = int(now)
            fresh.save()
    except OSError:
        pass  # a missing note never matters


def find(config: Config, prefix: str) -> list[dict]:
    """Trusted phones whose key fingerprint starts with prefix (colons and case ignored)."""
    want = prefix.replace(":", "").upper()
    return [p for p in config.phones if crypto.fingerprint(p["pub"]).replace(":", "").startswith(want)] if want else []


def open_pairing(config: Config, now: float | None = None) -> None:
    config.pairing_until = (now or time.time()) + PAIRING_WINDOW_S
