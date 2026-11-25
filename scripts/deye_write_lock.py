"""Durable per-inverter reservation backed by private Git ref updates."""

from contextlib import closing
import hashlib
import os
import re
from uuid import uuid4

if __package__:
    from .deye_private_store import GitHubPrivateStore, PrivateStoreError
else:
    from deye_private_store import GitHubPrivateStore, PrivateStoreError


class InverterBusy(ValueError):
    pass


def private_store_from_environment():
    return GitHubPrivateStore(
        os.environ.get("DEYE_DATA_REPOSITORY") or os.environ["GITHUB_REPOSITORY"],
        os.environ["GH_TOKEN"], public_repository=os.environ["DEYE_APPROVAL_CODE_REPOSITORY"],
    )


class InverterLock:
    def __init__(self, factory, serial, clock, *, execution_path=None):
        identity = hashlib.sha256(f"eu:{serial.casefold()}".encode()).hexdigest()
        self.path = f"state/inverter-locks/{identity}.json"
        self.execution_path = execution_path
        self.factory = factory
        self.clock = clock
        self.record = {"owner": uuid4().hex, "locked": True, "status": "validating"}

    def _publish(self, store, record):
        changes = {self.path: record}
        if self.execution_path:
            changes[self.execution_path] = record
        if not store.publish(changes):
            raise PrivateStoreError("INVERTER_LOCK_COMMIT_FAILED")

    def __enter__(self):
        with closing(self.factory()) as store:
            if self.execution_path and store.read(self.execution_path) is not None:
                raise InverterBusy("ALREADY_ATTEMPTED")
            existing = store.read(self.path)
            if existing is not None and existing.get("locked") is not False:
                raise InverterBusy("INVERTER_LOCKED")
            self.record["updated_at"] = self.clock().isoformat()
            self._publish(store, self.record)
        return self

    def checkpoint(self, status, *, locked=True, validate=None, **values):
        record = {**self.record, **values, "locked": locked, "status": status,
                  "updated_at": self.clock().isoformat()}
        with closing(self.factory()) as store:
            if store.read(self.path) != self.record:
                raise PrivateStoreError("INVERTER_LOCK_LOST")
            if validate is not None:
                validate(store)
            self._publish(store, record)
        self.record = record

    def __exit__(self, exc_type, exc, traceback):
        # A failed preflight cannot have sent a config change. Once submission
        # is recorded, leave ambiguous outcomes reserved for reconciliation.
        if self.record["status"] == "validating":
            reason = getattr(exc, "code", None)
            if isinstance(exc, ValueError) and re.fullmatch(r"[A-Z_]{1,80}", str(exc)):
                reason = str(exc)
            self.checkpoint("preflight_failed", locked=False,
                            reason=reason or (exc_type.__name__ if exc_type else None))
