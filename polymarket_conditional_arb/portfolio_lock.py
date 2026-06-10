from __future__ import annotations

import json
import os
import socket
import uuid
from pathlib import Path
from typing import Any, Mapping

from .event_log import utc_iso


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
            except FileExistsError:
                existing = self._read_existing()
                if self._is_stale_same_host_lock(existing):
                    try:
                        self.path.unlink()
                    except FileNotFoundError:
                        pass
                    except OSError as exc:
                        raise PortfolioLockError(f"failed to remove stale portfolio lock {self.path}: {exc}") from exc
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
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
        self._acquired = False

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
