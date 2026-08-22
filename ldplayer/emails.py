"""Guaranteed-unique email allocation for signup flows.

A JSON registry (``used_emails.json`` next to the package root) records every
address ever handed out, so the same email is never reused — across threads,
across parallel emulator instances, and across separate program runs.

The registry is also seeded from ``raw.txt`` (email|password lines written by
successful signups) so addresses used before this module existed stay unique.

Thread safety: a process-wide lock guards generation + registry writes.
Cross-process safety: writes go through an atomic replace, so concurrent
processes cannot corrupt the file (worst case a rare duplicate between
simultaneous processes; within one process it is impossible).
"""

from __future__ import annotations

import json
import random
import string
import threading

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REGISTRY_FILE = ROOT / "used_emails.json"
CREDENTIALS_FILE = ROOT / "raw.txt"

_EMAIL_DOMAIN = "dailykhabar.bond"

_lock = threading.Lock()
_registry: set[str] | None = None


def _load_registry() -> set[str]:
    global _registry
    if _registry is not None:
        return _registry

    reg: set[str] = set()

    # 1. persisted registry of everything ever generated
    if REGISTRY_FILE.is_file():
        try:
            data = json.loads(REGISTRY_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                reg.update(str(e).lower() for e in data)
        except (json.JSONDecodeError, OSError):
            pass

    # 2. seed from credentials history ("email|password" per line)
    if CREDENTIALS_FILE.is_file():
        try:
            for line in CREDENTIALS_FILE.read_text(
                    encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if "|" in line:
                    reg.add(line.split("|", 1)[0].strip().lower())
        except OSError:
            pass

    _registry = reg
    return _registry


def _save_registry(reg: set[str]) -> None:
    tmp = REGISTRY_FILE.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(sorted(reg), indent=1), encoding="utf-8")
        tmp.replace(REGISTRY_FILE)
    except OSError:
        pass  # uniqueness is still enforced in-memory


def generate_email(domain: str = _EMAIL_DOMAIN,
                   length: int = 7,
                   rng: random.Random | None = None) -> str:
    """Generate ONE random candidate address (no uniqueness check)."""
    r = rng or random
    letters = string.ascii_lowercase
    user = "".join(r.choices(letters, k=length))
    return f"{user}@{domain}"


def claim_email(domain: str = _EMAIL_DOMAIN,
                length: int = 7,
                max_tries: int = 1000) -> str:
    """Return an address that has NEVER been claimed before.

    Blocks briefly (lock) so 3 parallel workers can never receive the same
    address. The claim is persisted immediately, so even if the signup later
    fails and the instance is deleted, the address is never reused.
    """
    with _lock:
        reg = _load_registry()
        for _ in range(max_tries):
            email = generate_email(domain, length)
            if email.lower() not in reg:
                reg.add(email.lower())
                _save_registry(reg)
                return email
        raise RuntimeError("could not generate a unique email "
                           f"after {max_tries} tries")


def mark_used(email: str) -> None:
    """Explicitly record an address as used (e.g. one typed manually)."""
    with _lock:
        reg = _load_registry()
        reg.add(email.strip().lower())
        _save_registry(reg)


def known_count() -> int:
    with _lock:
        return len(_load_registry())
