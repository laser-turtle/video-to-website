"""Destination-aware disk capacity and crash-safe upload reservations.

Locations are logical library roots. Reservations are grouped by filesystem,
so two roots on the same disk cannot each promise the same remaining capacity.
An OS lock keeps each reservation alive; a crashed uploader's lock disappears.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .util import file_lock

GIB = 1024**3
DEFAULT_BUFFER = GIB
MAX_UPLOAD = 16 * GIB


def format_bytes(value: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{value:.0f} B"
        value /= 1024


def filesystem_id(path: Path) -> str:
    return str(path.stat().st_dev)


@dataclass(frozen=True)
class StorageLocation:
    id: str
    path: Path
    label: str = "Library"
    buffer_bytes: int = DEFAULT_BUFFER

    def __post_init__(self):
        if self.buffer_bytes < 0:
            raise ValueError("The free-space buffer cannot be negative.")
        object.__setattr__(self, "path", self.path.resolve())


class StorageFull(OSError):
    def __init__(self, required: int, status: dict):
        self.status = status
        super().__init__(
            f"Not enough free space for this upload. It needs {format_bytes(required)}; "
            f"{format_bytes(status['available_bytes'])} is available for new uploads "
            f"after the {format_bytes(status['buffer_bytes'])} free-space buffer "
            "and uploads already in progress."
        )


class StorageManager:
    def __init__(self, state: Path, locations: list[StorageLocation]):
        if not locations or len({location.id for location in locations}) != len(
            locations
        ):
            raise ValueError("Storage locations must have unique IDs.")
        self.locations = {location.id: location for location in locations}
        self.state = state.resolve()
        self.state.mkdir(parents=True, exist_ok=True)
        self.guard = self.state / "capacity.lock"

    def _destination(self, location_id: str, destination: Path | None):
        location = self.locations[location_id]
        if not location.path.is_dir():
            raise OSError("The library storage location is unavailable.")
        target = (destination or location.path).resolve()
        if not target.is_relative_to(location.path):
            raise ValueError("Upload destination is outside its library location.")
        # A new course has no directory yet. Its nearest existing parent tells
        # us which filesystem will actually receive the upload.
        while not target.exists() and target != location.path:
            target = target.parent
        if not target.is_dir():
            raise ValueError("Upload destination must be a directory.")
        return location, target

    def _reservations(self) -> list[dict]:
        active = []
        for path in self.state.glob("*.json"):
            with path.open("r+") as lease:
                try:
                    fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    alive = False
                except BlockingIOError:
                    alive = True
                try:
                    record = json.load(lease)
                except (ValueError, OSError):
                    if alive:
                        raise OSError("An active upload reservation could not be read.")
                    path.unlink(missing_ok=True)
                    continue
                partial = Path(record["partial"])
                if not alive:
                    # Only delete our own abandoned temporary file, never the
                    # committed source or an arbitrary path in the record.
                    if (
                        partial.name.startswith(".incoming-")
                        and not partial.is_symlink()
                        and partial.resolve().is_relative_to(
                            Path(record["root"]).resolve()
                        )
                    ):
                        partial.unlink(missing_ok=True)
                    path.unlink(missing_ok=True)
                    continue
                try:
                    written = partial.stat().st_size
                except FileNotFoundError:
                    written = 0
                active.append(
                    {**record, "remaining": max(0, record["bytes"] - written)}
                )
        return active

    def _status(self, location_id: str, destination: Path | None = None) -> dict:
        location, directory = self._destination(location_id, destination)
        device = filesystem_id(directory)
        # Read reservations first, then disk free space. Writes by these upload
        # handlers hold the same guard; external writers can only reduce the
        # capacity we observe. Every subsequent chunk rechecks it as well.
        active = [r for r in self._reservations() if r["filesystem_id"] == device]
        reserved = sum(r["remaining"] for r in active)
        buffers = [location.buffer_bytes] + [r["buffer_bytes"] for r in active]
        for other in self.locations.values():
            if other.path.is_dir() and filesystem_id(other.path) == device:
                buffers.append(other.buffer_bytes)
        buffer = max(buffers)
        disk = shutil.disk_usage(directory)
        free = max(0, disk.free)
        available = max(0, free - buffer - reserved)
        return {
            "id": location.id,
            "label": location.label,
            "filesystem_id": device,
            "total_bytes": disk.total,
            "used_bytes": max(0, disk.total - free),
            "free_bytes": free,
            "buffer_bytes": buffer,
            "reserved_bytes": reserved,
            "active_uploads": len(active),
            "available_bytes": available,
            "max_upload_bytes": MAX_UPLOAD,
            "accepting_uploads": available > 0,
            "low_space": available <= max(buffer, disk.total * 0.1),
            "updated": time.time(),
        }

    def status(self, location_id: str, destination: Path | None = None) -> dict:
        with file_lock(self.guard):
            return self._status(location_id, destination)

    def overview(self, location_id: str, destination: Path | None = None) -> dict:
        with file_lock(self.guard):
            return {
                "locations": [self._status(key) for key in self.locations],
                "destination": self._status(location_id, destination),
            }

    @contextlib.contextmanager
    def reserve(self, location_id: str, target: Path, length: int):
        if length <= 0 or length > MAX_UPLOAD:
            raise ValueError("Upload is empty or exceeds the maximum file size.")
        reservation = None
        partial = None
        lease = None
        output = None
        lease_path = self.state / (uuid.uuid4().hex + ".json")
        try:
            with file_lock(self.guard):
                status = self._status(location_id, target.parent)
                if length > status["available_bytes"]:
                    raise StorageFull(length, status)
                target.parent.mkdir(parents=True, exist_ok=True)
                fd, raw = tempfile.mkstemp(prefix=".incoming-", dir=target.parent)
                partial = Path(raw)
                output = os.fdopen(fd, "wb", buffering=0)
                lease = lease_path.open("x+")
                fcntl.flock(lease, fcntl.LOCK_EX)
                record = {
                    "location_id": location_id,
                    "root": str(self.locations[location_id].path),
                    "partial": str(partial),
                    "bytes": length,
                    "filesystem_id": status["filesystem_id"],
                    "buffer_bytes": status["buffer_bytes"],
                }
                json.dump(record, lease)
                lease.flush()
                os.fsync(lease.fileno())
                reservation = UploadReservation(
                    self, location_id, target, partial, output, length, lease_path
                )
            yield reservation
        finally:
            try:
                with file_lock(self.guard):
                    if output:
                        output.close()
                    if partial:
                        partial.unlink(missing_ok=True)
                    lease_path.unlink(missing_ok=True)
            finally:
                if output:
                    output.close()
                if lease:
                    lease.close()


class UploadReservation:
    def __init__(
        self, manager, location_id, target, partial, output, length, lease_path
    ):
        self.manager, self.location_id, self.target = manager, location_id, target
        self.partial, self.output, self.length = partial, output, length
        self.lease_path = lease_path
        self.received = 0

    def _check(self):
        status = self.manager._status(self.location_id, self.target.parent)
        if status["free_bytes"] < status["buffer_bytes"] + status["reserved_bytes"]:
            raise StorageFull(self.length - self.received, status)

    def write(self, chunk: bytes):
        with file_lock(self.manager.guard):
            self._check()
            if self.received + len(chunk) > self.length:
                raise ValueError("Upload body exceeds its declared size.")
            remaining = memoryview(chunk)
            while remaining:
                written = self.output.write(remaining)
                if not written:
                    raise OSError("Could not write the upload.")
                remaining = remaining[written:]
                self.received += written

    def commit(self):
        with file_lock(self.manager.guard):
            if self.received != self.length:
                raise OSError("Upload ended before the file was complete.")
            os.fchmod(self.output.fileno(), 0o644)
            os.fsync(self.output.fileno())
            self._check()
            self.partial.replace(self.target)
            self.lease_path.unlink()
