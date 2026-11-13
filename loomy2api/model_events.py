"""Append-only history of per-account model-entitlement changes.

The upstream scopes its model catalogue per account and silently substitutes a
free model for any id the account lacks.  That makes entitlement changes the
single most important thing to watch: an account quietly dropping from ten
models to the free pair changes what the whole gateway can serve, and the only
evidence is the catalogue itself.

``accounts.json`` keeps just the *current* list, so a downgrade that later
reverses leaves no trace.  This module records each transition as one JSONL
line and keeps a rolling window (7 days by default), which is what lets an
operator answer "did this account come back, and when did it break?".

Format, one JSON object per line::

    {"at": 1794580644, "account": "138****0001", "event": "downgrade",
     "lost": ["GLM-5.3-Flash"], "gained": [], "from": [...], "to": [...]}

``from``/``to`` store only the ids an account can actually *use* — spark-style
free models included — because that is the list routing filters on.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

#: Records older than this are dropped on the next prune.
RETENTION_SECONDS = 7 * 24 * 3600

#: How often the retention window is applied.  Rewriting the file on every
#: probe would be pure overhead when nothing has aged out.
PRUNE_INTERVAL = 3600

#: A model is "free" when its id carries this prefix (``spark-x2.6``,
#: ``spark-x2.6-low``, ``spark-x2.6-4b`` …).  Kept in sync with the panel.
FREE_PREFIX = "spark-"


def is_free(model: str) -> bool:
    return str(model).lower().startswith(FREE_PREFIX)


def classify(before: Iterable[str], after: Iterable[str]) -> Optional[str]:
    """Return the event that ``before → after`` represents, or ``None``.

    Only paid models matter: the upstream renames and splits its free ones
    (measured: ``spark-x2.6`` became ``spark-x2.6-low`` + ``spark-x2.6-4b``),
    so treating a free-id shuffle as a change would bury the real signal.
    """
    old = {m for m in before}
    new = {m for m in after}
    if old == new:
        return None
    if not new:
        # Everything is gone — including the free tier the account was at
        # least able to use.  Without this the account keeps showing its last
        # "recover" event while being able to serve nothing at all.
        return "downgrade"
    lost = {m for m in old - new if not is_free(m)}
    gained = {m for m in new - old if not is_free(m)}
    if lost:
        return "downgrade"
    if gained:
        return "recover"
    return None


class ModelEventLog:
    """Rolling, append-only entitlement history (7 days by default)."""

    def __init__(self, path: Path, *, retention: float = RETENTION_SECONDS):
        self.path = Path(path)
        self.retention = float(retention)
        self._lock = threading.Lock()
        self._last_prune = 0.0

    # -- writing --------------------------------------------------------

    def record(self, account: str, event: str, before: Iterable[str],
               after: Iterable[str], *, at: Optional[float] = None) -> Dict[str, Any]:
        old, new = sorted(set(before)), sorted(set(after))
        before_set, after_set = set(old), set(new)
        entry = {
            "at": int(at if at is not None else time.time()),
            "account": str(account),
            "event": str(event),
            "lost": sorted(m for m in before_set - after_set if not is_free(m)),
            "gained": sorted(m for m in after_set - before_set if not is_free(m)),
            "from": old,
            "to": new,
        }
        if not new and event == "downgrade":
            # Nothing left at all.  ``lost`` only lists paid models, so this
            # event would otherwise render as "失去 （现 0 个模型）".
            entry["note"] = "全部模型不可用"
        self.append(entry)
        return entry

    def append(self, entry: Dict[str, Any]) -> None:
        line = json.dumps(entry, ensure_ascii=False)
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError:
                return                       # diagnostics must never break routing
            self._prune_locked()

    def _prune_locked(self) -> None:
        now = time.time()
        if now - self._last_prune < PRUNE_INTERVAL:
            return
        self._last_prune = now
        # Rewriting the file is only safe when no other process (the CLI's
        # ``models`` command probes too) is appending to it.  A lock file makes
        # the read-modify-replace atomic; if it cannot be taken, skip the pass
        # and try again next hour — retention is a diagnostic concern, not a
        # correctness one.
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        with self._file_lock(lock_path) as held:
            if not held:
                return
            cutoff = now - self.retention
            try:
                size_before = self.path.stat().st_size
            except OSError:
                return
            rows = self._read_locked()
            if not rows:
                return
            keep = [r for r in rows if float(r.get("at") or 0) >= cutoff]
            if len(keep) == len(rows):
                return
            # Appends are single ``O_APPEND`` writes and hold no lock, so a row
            # can land between the read above and the replace below.  Rewriting
            # would drop it, so re-check the size: any growth means someone
            # wrote concurrently and this pass yields to the next one.
            try:
                if self.path.stat().st_size != size_before:
                    return
            except OSError:
                return
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            try:
                tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n"
                                       for r in keep), encoding="utf-8")
                os.replace(tmp, self.path)
            except OSError:
                pass

    @staticmethod
    def _file_lock(path: Path):
        """Best-effort exclusive lock on ``path`` (no third-party deps)."""
        class _Lock:
            def __init__(self) -> None:
                self.fh = None

            def __enter__(self) -> bool:
                try:
                    self.fh = path.open("a+")
                    if os.name == "nt":
                        import msvcrt
                        self.fh.seek(0)
                        msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return True
                except OSError:
                    if self.fh is not None:
                        try:
                            self.fh.close()
                        except OSError:
                            pass
                        self.fh = None
                    return False

            def __exit__(self, *_exc) -> None:
                if self.fh is None:
                    return
                try:
                    if os.name == "nt":
                        import msvcrt
                        self.fh.seek(0)
                        msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
                try:
                    self.fh.close()
                except OSError:
                    pass
                self.fh = None

        return _Lock()

    # -- reading --------------------------------------------------------

    def _read_locked(self) -> List[Dict[str, Any]]:
        try:
            raw = self.path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        rows = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:                             # noqa: BLE001
                continue
            if isinstance(obj, dict) and obj.get("account"):
                rows.append(obj)
        return rows

    def events(self, *, account: str = "", days: float = 7,
               limit: int = 500) -> List[Dict[str, Any]]:
        """Newest-first events inside the retention window."""
        cutoff = time.time() - max(1.0, float(days)) * 86400
        with self._lock:
            rows = self._read_locked()
        rows = [r for r in rows if float(r.get("at") or 0) >= cutoff]
        if account:
            rows = [r for r in rows if r.get("account") == account]
        rows.sort(key=lambda r: float(r.get("at") or 0), reverse=True)
        return rows[:max(1, int(limit))]

    def summary(self, *, days: float = 7) -> Dict[str, Any]:
        """Per-account current state derived from the history.

        ``since`` is the oldest event kept for that account, so the UI can say
        how long it has been downgraded rather than only that it is.
        """
        with self._lock:
            rows = self._read_locked()
        cutoff = time.time() - max(1.0, float(days)) * 86400
        rows = [r for r in rows if float(r.get("at") or 0) >= cutoff]
        by_account: Dict[str, List[Dict[str, Any]]] = {}
        for r in rows:
            by_account.setdefault(str(r.get("account")), []).append(r)
        out: Dict[str, Any] = {}
        for name, items in by_account.items():
            items.sort(key=lambda r: float(r.get("at") or 0))
            last = items[-1]
            out[name] = {
                "event": last.get("event"),
                "at": last.get("at"),
                "lost": last.get("lost") or [],
                "gained": last.get("gained") or [],
                "models": last.get("to") or [],
                "downgrades": sum(1 for r in items if r.get("event") == "downgrade"),
                "recovers": sum(1 for r in items if r.get("event") == "recover"),
                "first_seen": items[0].get("at"),
            }
        return out

    def clear(self) -> None:
        with self._lock:
            try:
                self.path.write_text("", encoding="utf-8")
            except OSError:
                pass