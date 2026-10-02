"""Patch 19: bounded, compressed, rotating recorder of the live frame stream.

Every frame the engine's RunLoop sees (program/screen/shard rows, websocket
orderbook snapshots/deltas, public trades for the subscribed candidate set,
which includes every quoted market) is appended as one JSON line to a gzip
file under LIP_RECORD_DIR (default /var/lib/lip-maker/recordings).

* Off unless LIP_RECORD_ENABLE=1.
* Serialization happens on the caller thread (cheap); compression and disk
  I/O on a daemon writer thread behind a bounded queue. If the queue is full
  the frame is dropped and counted (``dropped``); the trading path never
  blocks on disk.
* Rotation: every LIP_RECORD_ROTATE_S (3600) or LIP_RECORD_ROTATE_MB (256 MB
  uncompressed). Each new file starts with the current program/screen rows
  (``"hdr": 1``) so a single file replays on its own.
* Retention on start and every rotation: delete files older than
  LIP_RECORD_RETENTION_DAYS (14), then oldest-first until the directory is
  under LIP_RECORD_MAX_GB (5). A low-disk guard stops writing if the
  filesystem has < LIP_RECORD_MIN_FREE_GB (10) free.
* gzip is flushed (Z_SYNC_FLUSH) every LIP_RECORD_FLUSH_S (5 s): a crash
  loses at most a few seconds; readers tolerate a truncated tail.
* ``stop(timeout)`` (default 5 s) writes the frames still queued before the
  writer exits, for at most ``timeout`` seconds; what is left after that is
  counted in ``dropped``. A SIGKILL / crash skips this (see above).
"""
from __future__ import annotations

import gzip
import json
import logging
import os
import queue
import shutil
import threading
import time
from pathlib import Path

_log = logging.getLogger("lip.recorder")
PREFIX = "frames-"
SUFFIX = ".jsonl.gz"
HEADER_KINDS = ("program", "screen", "shard")


def _num(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


def enabled() -> bool:
    return _num("LIP_RECORD_ENABLE", 0.0) > 0


def list_files(directory) -> list:
    d = Path(directory)
    if not d.is_dir():
        return []
    return sorted(p for p in d.iterdir() if p.name.startswith(PREFIX) and p.name.endswith(SUFFIX))


def enforce_retention(directory, *, max_bytes: float, retention_s: float,
                      now: float | None = None, keep: Path | None = None) -> list:
    """Delete expired files, then oldest-first until total <= max_bytes. Returns deleted names."""
    now = time.time() if now is None else now
    deleted = []
    files = list_files(directory)
    for p in list(files):
        if p == keep:
            continue
        try:
            if retention_s > 0 and now - p.stat().st_mtime > retention_s:
                p.unlink()
                deleted.append(p.name)
                files.remove(p)
        except FileNotFoundError:
            files.remove(p)
    total = sum(p.stat().st_size for p in files if p.exists())
    for p in files:
        if total <= max_bytes:
            break
        if p == keep:
            continue
        try:
            size = p.stat().st_size
            p.unlink()
            deleted.append(p.name)
            total -= size
        except FileNotFoundError:
            pass
    return deleted


class FrameRecorder:
    def __init__(self, directory: str | None = None, *, max_gb: float | None = None,
                 retention_days: float | None = None, rotate_s: float | None = None,
                 rotate_mb: float | None = None, flush_s: float | None = None,
                 min_free_gb: float | None = None, queue_max: int = 200_000) -> None:
        self.dir = Path(directory or os.environ.get("LIP_RECORD_DIR", "/var/lib/lip-maker/recordings"))
        self.max_bytes = (max_gb if max_gb is not None else _num("LIP_RECORD_MAX_GB", 5.0)) * 1e9
        self.retention_s = (retention_days if retention_days is not None
                            else _num("LIP_RECORD_RETENTION_DAYS", 14.0)) * 86400.0
        self.rotate_s = rotate_s if rotate_s is not None else _num("LIP_RECORD_ROTATE_S", 3600.0)
        self.rotate_bytes = (rotate_mb if rotate_mb is not None else _num("LIP_RECORD_ROTATE_MB", 256.0)) * 1e6
        self.flush_s = flush_s if flush_s is not None else _num("LIP_RECORD_FLUSH_S", 5.0)
        self.min_free = (min_free_gb if min_free_gb is not None else _num("LIP_RECORD_MIN_FREE_GB", 10.0)) * 1e9
        self.q: queue.Queue = queue.Queue(maxsize=queue_max)
        self.header: dict = {}
        self._hlock = threading.Lock()
        self.stats = {"frames": 0, "dropped": 0, "files_opened": 0, "deleted": 0,
                      "raw_bytes": 0, "errors": 0, "paused_low_disk": False,
                      "started_ts": time.time(), "current": None}
        self._fh = None
        self._opened_at = 0.0
        self._raw_in_file = 0
        self._last_flush = 0.0
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._drain_until = 0.0

    # caller thread -------------------------------------------------------
    def record(self, frame: dict) -> None:
        try:
            kind = str(frame.get("kind") or frame.get("type") or "")
            if kind in HEADER_KINDS:
                key = (kind, str(frame.get("market") or ""))
                with self._hlock:
                    self.header[key] = frame
            line = json.dumps(frame, separators=(",", ":"), default=str)
            self.q.put_nowait(line)
        except queue.Full:
            self.stats["dropped"] += 1
        except Exception:
            self.stats["errors"] += 1

    def start(self) -> "FrameRecorder":
        self.dir.mkdir(parents=True, exist_ok=True)
        self.stats["deleted"] += len(enforce_retention(self.dir, max_bytes=self.max_bytes,
                                                       retention_s=self.retention_s))
        self._thread = threading.Thread(target=self._run, name="lip-recorder", daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the writer after it has written the queued frames, waiting
        at most ``timeout`` seconds for that; frames still queued then are
        counted as dropped. The file is closed by the writer thread (or here
        when no writer is running)."""
        self._drain_until = time.time() + max(0.0, float(timeout))
        self._stop.set()
        if self._thread is not None:
            self._thread.join(max(0.0, float(timeout)) + 1.0)
            if self._thread.is_alive():
                # Writer stuck (disk I/O): never close its file under it.
                _log.warning("recorder writer did not stop within %.1fs; %d frames left queued",
                             timeout, self.q.qsize())
                return
        left = self.q.qsize()
        if left:
            self.stats["dropped"] += left
        self._close()

    # writer thread -------------------------------------------------------
    def _open(self, now: float) -> None:
        self._close()
        name = PREFIX + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now)) + SUFFIX
        path = self.dir / name
        n = 1
        while path.exists():
            path = self.dir / (name[:-len(SUFFIX)] + f"-{n}" + SUFFIX)
            n += 1
        self._fh = gzip.open(path, "wt", encoding="utf-8", compresslevel=6)
        self._path = path
        self._opened_at = now
        self._raw_in_file = 0
        self.stats["files_opened"] += 1
        self.stats["current"] = path.name
        with self._hlock:
            rows = list(self.header.values())
        for row in rows:
            line = json.dumps(dict(row, hdr=1), separators=(",", ":"), default=str)
            self._fh.write(line + "\n")
            self._raw_in_file += len(line) + 1
        self.stats["deleted"] += len(enforce_retention(
            self.dir, max_bytes=self.max_bytes, retention_s=self.retention_s, now=now, keep=path))

    def _close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except Exception:
                self.stats["errors"] += 1
            self._fh = None

    def _low_disk(self) -> bool:
        try:
            return shutil.disk_usage(self.dir).free < self.min_free
        except Exception:
            return False

    def _run(self) -> None:
        while True:
            if self._stop.is_set() and (self.q.empty() or time.time() >= self._drain_until):
                break
            try:
                line = self.q.get(timeout=0.05 if self._stop.is_set() else 1.0)
            except queue.Empty:
                line = None
            now = time.time()
            try:
                if line is not None:
                    if self._fh is None or now - self._opened_at >= self.rotate_s \
                            or self._raw_in_file >= self.rotate_bytes:
                        low = self._low_disk()
                        self.stats["paused_low_disk"] = low
                        if low:
                            self._close()
                            self.stats["dropped"] += 1
                            continue
                        self._open(now)
                    self._fh.write(line + "\n")
                    self._raw_in_file += len(line) + 1
                    self.stats["raw_bytes"] += len(line) + 1
                    self.stats["frames"] += 1
                if self._fh is not None and now - self._last_flush >= self.flush_s:
                    self._fh.flush()
                    self._last_flush = now
            except Exception:
                self.stats["errors"] += 1
                _log.exception("recorder write failed")
                self._close()
        self._close()

    def summary(self) -> dict:
        files = list_files(self.dir)
        total = 0
        for p in files:
            try:
                total += p.stat().st_size
            except FileNotFoundError:
                pass
        el = max(1.0, time.time() - self.stats["started_ts"])
        out = {k: v for k, v in self.stats.items()}
        out.update({"enabled": True, "dir": str(self.dir), "files": len(files),
                    "disk_bytes": total, "queue": self.q.qsize(),
                    "max_gb": round(self.max_bytes / 1e9, 3),
                    "retention_days": round(self.retention_s / 86400.0, 2)})
        out["raw_mb_per_hour"] = round(self.stats["raw_bytes"] / el * 3600 / 1e6, 2)
        return out


def iter_frames(paths):
    """Yield frames from recording files in order. Tolerates a truncated gzip tail."""
    for p in paths:
        try:
            with gzip.open(p, "rt", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except ValueError:
                        break  # partial last line
        except (EOFError, OSError):
            continue  # truncated tail / file being written: keep what was read
