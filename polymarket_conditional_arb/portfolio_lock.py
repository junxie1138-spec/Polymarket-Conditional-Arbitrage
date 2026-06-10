from __future__ import annotations

import json
import os
import socket
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

from .event_log import utc_iso

LOCK_UNLINK_ATTEMPTS = 8
LOCK_UNLINK_BACKOFF_SECONDS = 0.01


class PortfolioLockError(RuntimeError):
    pass


class PortfolioDataLock:
    def __init__(self, state_path: str | Path, *, lock_path: str | Path | None = None) -> None:
        self.state_path = Path(state_path)
        self.path = (
            Path(lock_path)
            if lock_path is not None
            else self.state_path.with_name(self.state_path.name + ".lock")
        )
        self.host = socket.gethostname()
        self.pid = os.getpid()
        self.token = str(uuid.uuid4())
        self._acquired = False

    def __enter__(self) -> "PortfolioDataLock":
        return self.acquire()

    def __exit__(self, *_exc_info: object) -> None:
        self.release()

    def acquire(self) -> "PortfolioDataLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except PermissionError as exc:
                raise PortfolioLockError(self._locked_message(self._read_existing())) from exc
            except FileExistsError:
                existing = self._read_existing()
                if self._is_stale_same_host_lock(existing):
                    removed = self._unlink_lock_matching_token(
                        str(existing.get("token") or ""),
                        error_context="remove stale portfolio lock",
                    )
                    if not removed:
                        raise PortfolioLockError(self._locked_message(self._read_existing()))
                    continue
                raise PortfolioLockError(self._locked_message(existing))

            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                json.dump(self._metadata(), f, separators=(",", ":"), sort_keys=True)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            self._acquired = True
            return self

    def release(self) -> None:
        if not self._acquired:
            return
        existing = self._read_existing()
        if existing.get("token") == self.token:
            self._unlink_lock_matching_token(self.token, error_context="release portfolio lock")
        self._acquired = False

    def _unlink_lock_matching_token(self, expected_token: str, *, error_context: str) -> bool:
        if not expected_token:
            return False
        delay = LOCK_UNLINK_BACKOFF_SECONDS
        for attempt in range(LOCK_UNLINK_ATTEMPTS):
            existing = self._read_existing()
            if existing.get("token") != expected_token:
                return False
            try:
                self.path.unlink()
                return True
            except FileNotFoundError:
                return True
            except PermissionError as exc:
                if attempt + 1 >= LOCK_UNLINK_ATTEMPTS:
                    raise PortfolioLockError(f"failed to {error_context} {self.path}: {exc}") from exc
                time.sleep(delay)
                delay *= 2
            except OSError as exc:
                raise PortfolioLockError(f"failed to {error_context} {self.path}: {exc}") from exc
        return False

    def _metadata(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "host": self.host,
            "pid": self.pid,
            "token": self.token,
            "state_path": str(self.state_path),
            "created_at_utc": utc_iso(),
        }

    def _read_existing(self) -> dict[str, Any]:
        try:
            with self.path.open(encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}
        return dict(data) if isinstance(data, Mapping) else {}

    def _is_stale_same_host_lock(self, data: Mapping[str, Any]) -> bool:
        if str(data.get("host") or "") != self.host:
            return False
        try:
            pid = int(data.get("pid"))
        except (TypeError, ValueError):
            return False
        return pid > 0 and not self._process_is_alive(pid)

    @staticmethod
    def _process_is_alive(pid: int) -> bool:
        if pid == os.getpid():
            return True
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    def _locked_message(self, data: Mapping[str, Any]) -> str:
        if data:
            owner = f"pid={data.get('pid')} host={data.get('host')} created_at_utc={data.get('created_at_utc')}"
        else:
            owner = "unreadable owner metadata"
        return f"paper portfolio data is locked ({owner}); lock_path={self.path}"
