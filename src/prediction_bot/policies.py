"""Immutable local policy registry; operational state is not configuration state."""

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile

from prediction_bot.settings import Settings, load_settings, policy_payload


class PolicyStore:
    """Publish complete content-addressed files without overwriting old versions.

    A hard link atomically publishes a fully flushed temporary file. An existing
    digest must have identical bytes; tampered records are never silently repaired.
    The directory is private local state, not an untrusted shared filesystem.
    """

    def __init__(self, directory: Path):
        self.directory = directory

    def save(self, settings: Settings) -> str:
        payload = policy_payload(settings)
        version = hashlib.sha256(payload).hexdigest()
        self.directory.mkdir(parents=True, exist_ok=True)
        destination = self.directory / f"{version}.json"
        with NamedTemporaryFile(dir=self.directory, delete=False) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
        try:
            try:
                os.link(temporary_path, destination)
            except FileExistsError:
                if destination.read_bytes() != payload:
                    raise ValueError("Policy integrity check failed") from None
        finally:
            temporary_path.unlink(missing_ok=True)
        return version

    def get(self, version: str) -> Settings:
        if re.fullmatch(r"[a-f0-9]{64}", version) is None:
            raise ValueError("Invalid policy version")
        path = self.directory / f"{version}.json"
        if hashlib.sha256(path.read_bytes()).hexdigest() != version:
            raise ValueError("Policy integrity check failed")
        return load_settings(path)


@dataclass(frozen=True)
class SessionPolicy:
    session_id: str
    version: str


class SettingsManager:
    """Reload stages a version; callers retain their persisted session binding.

    This class has no access to order queues, latches, counters or broker state.
    Session bindings are persisted before return and reused across restarts.
    The future exchange-session coordinator owns eligibility for a new session.
    """

    def __init__(self, store: PolicyStore, settings: Settings):
        self.store = store
        self.pending_version = store.save(settings)

    def reload(self, path: Path) -> str:
        version = self.store.save(load_settings(path))
        self.pending_version = version
        return version

    def bind_new_session(self, session_id: str) -> SessionPolicy:
        if not session_id.strip():
            raise ValueError("Session identifier is required")
        directory = self.store.directory / "sessions"
        directory.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha256(session_id.encode()).hexdigest()
        destination = directory / key
        with NamedTemporaryFile(dir=directory, delete=False) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(self.pending_version.encode("ascii"))
            temporary.flush()
            os.fsync(temporary.fileno())
        try:
            try:
                os.link(temporary_path, destination)
            except FileExistsError:
                pass
        finally:
            temporary_path.unlink(missing_ok=True)
        version = destination.read_text(encoding="ascii")
        self.store.get(version)  # Fail closed on a damaged binding or policy.
        return SessionPolicy(session_id, version)

    def resolve(self, binding: SessionPolicy) -> Settings:
        return self.store.get(binding.version)
