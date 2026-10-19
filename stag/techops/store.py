"""Control store: the transactional home of observations, incidents, requests and operations.

The backbone's *logic* (state machines, material-change rules, ledger transitions) stays pure and
in-memory-testable. The *state* lives behind this interface so that:

* two workers racing on the same input produce one committed effect (unique keys, compare-and-swap),
* a restart loses nothing that was committed (SQLite locally, PostgreSQL in CI/production),
* queued work (investigation requests) can be claimed with a lease and recovered after a crash.

Documents are JSON. Each row carries a *row version* for compare-and-swap that is independent of
the incident's own evidence version. ``InMemoryControlStore`` keeps the exact objects it is given
(so the simulation runner and unit tests keep object identity); ``SqlControlStore`` serialises.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel


class ConflictError(Exception):
    """Compare-and-swap failed: the row changed since it was read."""


def _now() -> datetime:
    return datetime.now(UTC)


def _dump(doc: Any) -> str:
    if isinstance(doc, BaseModel):
        return doc.model_dump_json()
    return json.dumps(doc, default=str, separators=(",", ":"))


class ControlStore(ABC):
    """Minimal document store with unique insert, CAS update, status/scope listing and leases."""

    # ---------------------------------------------------------------- transactions
    @abstractmethod
    @contextmanager
    def transaction(self) -> Iterator[None]: ...

    # ---------------------------------------------------------------- documents
    @abstractmethod
    def insert_unique(self, collection: str, key: str, doc: Any, *, status: str = "", scope: str = "") -> bool:
        """Insert if absent. Returns False when the key already exists (no change)."""

    @abstractmethod
    def get(self, collection: str, key: str) -> tuple[Any, int] | None:
        """Return (doc, row_version) or None."""

    @abstractmethod
    def put(self, collection: str, key: str, doc: Any, expected_version: int | None, *, status: str = "", scope: str = "") -> int:
        """Upsert with CAS. ``expected_version=None`` means create-or-replace. Returns the new row version."""

    @abstractmethod
    def delete(self, collection: str, key: str) -> bool: ...

    @abstractmethod
    def list(self, collection: str, *, status: str | None = None, scope: str | None = None, limit: int | None = None) -> list[tuple[str, Any, int]]:
        """Return (key, doc, row_version) tuples, oldest first."""

    @abstractmethod
    def count(self, collection: str) -> int: ...

    @abstractmethod
    def append(self, collection: str, doc: Any) -> int:
        """Append-only log (quarantine, audit). Returns the row id."""

    @abstractmethod
    def purge_older_than(self, collection: str, cutoff: datetime) -> int: ...

    # ---------------------------------------------------------------- leases
    @abstractmethod
    def claim(self, collection: str, key: str, owner: str, lease_seconds: int, *, from_statuses: tuple[str, ...]) -> tuple[Any, int] | None:
        """Atomically move a document to status ``running`` with a lease if its status is in
        ``from_statuses`` or its lease has expired. Returns (doc, row_version) or None."""

    # ---------------------------------------------------------------- helpers
    def materialize(self, doc: Any, model: type[BaseModel]) -> BaseModel:
        if isinstance(doc, model):
            return doc
        if isinstance(doc, str):
            return model.model_validate_json(doc)
        return model.model_validate(doc)


# =============================================================================== in-memory


class InMemoryControlStore(ControlStore):
    """Process-local store. Keeps the objects it is given so callers can mutate in place."""

    def __init__(self) -> None:
        self._docs: dict[str, dict[str, dict[str, Any]]] = {}
        self._logs: dict[str, list[tuple[int, datetime, Any]]] = {}
        self._lock = threading.RLock()
        self._log_seq = 0
        self._depth = 0

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Row-level atomicity: inserts, puts, deletes and claims made inside the block are undone
        on exception. (Mutations made *inside* objects held by rows are the caller's to undo.)"""
        with self._lock:
            if self._depth:
                self._depth += 1
                try:
                    yield
                finally:
                    self._depth -= 1
                return
            snapshot = {c: {k: dict(r) for k, r in coll.items()} for c, coll in self._docs.items()}
            log_len = {c: len(v) for c, v in self._logs.items()}
            self._depth = 1
            try:
                yield
            except BaseException:
                self._docs = snapshot
                for c, n in log_len.items():
                    del self._logs[c][n:]
                for c in list(self._logs):
                    if c not in log_len:
                        del self._logs[c]
                raise
            finally:
                self._depth = 0

    def _coll(self, collection: str) -> dict[str, dict[str, Any]]:
        return self._docs.setdefault(collection, {})

    def insert_unique(self, collection, key, doc, *, status="", scope=""):
        with self._lock:
            coll = self._coll(collection)
            if key in coll:
                return False
            coll[key] = {"doc": doc, "version": 1, "status": status, "scope": scope, "updated_at": _now(), "lease_until": None, "owner": None}
            return True

    def get(self, collection, key):
        with self._lock:
            row = self._coll(collection).get(key)
            return (row["doc"], row["version"]) if row else None

    def put(self, collection, key, doc, expected_version, *, status="", scope=""):
        with self._lock:
            coll = self._coll(collection)
            row = coll.get(key)
            if expected_version is not None and (row is None or row["version"] != expected_version):
                raise ConflictError(f"{collection}/{key}: expected version {expected_version}, found {row['version'] if row else None}")
            version = (row["version"] + 1) if row else 1
            coll[key] = {"doc": doc, "version": version, "status": status or (row["status"] if row else ""), "scope": scope or (row["scope"] if row else ""), "updated_at": _now(), "lease_until": row["lease_until"] if row else None, "owner": row["owner"] if row else None}
            return version

    def delete(self, collection, key):
        with self._lock:
            return self._coll(collection).pop(key, None) is not None

    def list(self, collection, *, status=None, scope=None, limit=None):
        with self._lock:
            rows = [(k, r) for k, r in self._coll(collection).items() if (status is None or r["status"] == status) and (scope is None or r["scope"] == scope)]
            rows.sort(key=lambda kr: kr[1]["updated_at"])
            out = [(k, r["doc"], r["version"]) for k, r in rows]
            return out[:limit] if limit else out

    def count(self, collection):
        with self._lock:
            return len(self._coll(collection))

    def append(self, collection, doc):
        with self._lock:
            self._log_seq += 1
            self._logs.setdefault(collection, []).append((self._log_seq, _now(), doc))
            return self._log_seq

    def log(self, collection: str) -> list[Any]:
        with self._lock:
            return [d for _, _, d in self._logs.get(collection, [])]

    def purge_older_than(self, collection, cutoff):
        with self._lock:
            coll = self._coll(collection)
            stale = [k for k, r in coll.items() if r["updated_at"] < cutoff]
            for k in stale:
                del coll[k]
            return len(stale)

    def claim(self, collection, key, owner, lease_seconds, *, from_statuses):
        with self._lock:
            row = self._coll(collection).get(key)
            if row is None:
                return None
            now = _now()
            expired = row["status"] == "running" and row["lease_until"] is not None and row["lease_until"] < now
            if row["status"] not in from_statuses and not expired:
                return None
            row["status"] = "running"
            row["owner"] = owner
            row["lease_until"] = now + timedelta(seconds=lease_seconds)
            row["version"] += 1
            row["updated_at"] = now
            return row["doc"], row["version"]

    def status_of(self, collection: str, key: str) -> str | None:
        with self._lock:
            row = self._coll(collection).get(key)
            return row["status"] if row else None


# =============================================================================== SQL (SQLite / PostgreSQL)

_SCHEMA_SQLITE = """
CREATE TABLE IF NOT EXISTS control_docs (
    collection TEXT NOT NULL, key TEXT NOT NULL, version INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT '', scope TEXT NOT NULL DEFAULT '',
    owner TEXT, lease_until TEXT, updated_at TEXT NOT NULL, doc TEXT NOT NULL,
    PRIMARY KEY (collection, key)
);
CREATE INDEX IF NOT EXISTS control_docs_status ON control_docs (collection, status, updated_at);
CREATE INDEX IF NOT EXISTS control_docs_scope ON control_docs (collection, scope);
CREATE TABLE IF NOT EXISTS control_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, collection TEXT NOT NULL, created_at TEXT NOT NULL, doc TEXT NOT NULL
);
"""

_SCHEMA_POSTGRES = """
CREATE TABLE IF NOT EXISTS control_docs (
    collection TEXT NOT NULL, key TEXT NOT NULL, version INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT '', scope TEXT NOT NULL DEFAULT '',
    owner TEXT, lease_until TIMESTAMPTZ, updated_at TIMESTAMPTZ NOT NULL, doc TEXT NOT NULL,
    PRIMARY KEY (collection, key)
);
CREATE INDEX IF NOT EXISTS control_docs_status ON control_docs (collection, status, updated_at);
CREATE INDEX IF NOT EXISTS control_docs_scope ON control_docs (collection, scope);
CREATE TABLE IF NOT EXISTS control_log (
    id BIGSERIAL PRIMARY KEY, collection TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL, doc TEXT NOT NULL
);
"""


class SqlControlStore(ControlStore):
    """DB-API control store. ``url`` is ``sqlite:///path`` (or ``sqlite:///:memory:``) or a
    PostgreSQL DSN (``postgresql://...``, needs ``psycopg``). One connection per instance; a
    process-wide lock serialises use of that connection. Documents are stored as JSON text.
    """

    def __init__(self, url: str) -> None:
        self.url = url
        self._lock = threading.RLock()
        self._depth = 0
        if url.startswith("sqlite://"):
            path = url[len("sqlite://"):]
            if path.startswith("/") and not path.startswith("//"):
                path = path[1:] if path != "/:memory:" else ":memory:"
            self.dialect = "sqlite"
            self._conn = sqlite3.connect(path or ":memory:", isolation_level=None, check_same_thread=False, timeout=10.0)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=10000")
            self._conn.executescript(_SCHEMA_SQLITE)
            self._p = "?"
        elif url.startswith(("postgresql://", "postgres://")):
            try:
                import psycopg
            except ImportError as e:  # pragma: no cover
                raise RuntimeError("psycopg is required for a PostgreSQL control store: pip install 'psycopg[binary]'") from e
            self.dialect = "postgres"
            self._conn = psycopg.connect(url, autocommit=True)
            with self._conn.cursor() as cur:
                cur.execute(_SCHEMA_POSTGRES)
            self._p = "%s"
        else:
            raise ValueError("control store url must start with sqlite:// or postgresql://")

    # ---------------------------------------------------------------- plumbing
    def _exec(self, sql: str, params: tuple = ()):
        sql = sql.replace("?", self._p) if self._p != "?" else sql
        cur = self._conn.cursor()
        cur.execute(sql, params)
        return cur

    @staticmethod
    def _ts(value: datetime | None) -> str | None:
        return value.isoformat() if value else None

    @staticmethod
    def _parse_ts(value) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=UTC)
        return datetime.fromisoformat(value)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self._lock:
            if self._depth == 0:
                self._exec("BEGIN IMMEDIATE" if self.dialect == "sqlite" else "BEGIN")
            self._depth += 1
            try:
                yield
            except BaseException:
                self._depth -= 1
                if self._depth == 0:
                    self._exec("ROLLBACK")
                raise
            else:
                self._depth -= 1
                if self._depth == 0:
                    self._exec("COMMIT")

    # ---------------------------------------------------------------- documents
    def insert_unique(self, collection, key, doc, *, status="", scope=""):
        with self._lock:
            sql = "INSERT INTO control_docs (collection, key, version, status, scope, updated_at, doc) VALUES (?,?,1,?,?,?,?) ON CONFLICT (collection, key) DO NOTHING"
            cur = self._exec(sql, (collection, key, status, scope, self._ts(_now()), _dump(doc)))
            return cur.rowcount == 1

    def get(self, collection, key):
        with self._lock:
            row = self._exec("SELECT doc, version FROM control_docs WHERE collection=? AND key=?", (collection, key)).fetchone()
            return (row[0], row[1]) if row else None

    def put(self, collection, key, doc, expected_version, *, status="", scope=""):
        with self._lock:
            now = self._ts(_now())
            if expected_version is None:
                existing = self.get(collection, key)
                if existing is None:
                    self._exec("INSERT INTO control_docs (collection, key, version, status, scope, updated_at, doc) VALUES (?,?,1,?,?,?,?)",
                               (collection, key, status, scope, now, _dump(doc)))
                    return 1
                expected_version = existing[1]
            status_sql = ", status=?" if status else ""
            scope_sql = ", scope=?" if scope else ""
            params: list[Any] = [_dump(doc), now]
            if status:
                params.append(status)
            if scope:
                params.append(scope)
            params += [collection, key, expected_version]
            cur = self._exec(f"UPDATE control_docs SET doc=?, updated_at=?, version=version+1{status_sql}{scope_sql} WHERE collection=? AND key=? AND version=?", tuple(params))
            if cur.rowcount != 1:
                raise ConflictError(f"{collection}/{key}: expected version {expected_version}")
            return expected_version + 1

    def delete(self, collection, key):
        with self._lock:
            return self._exec("DELETE FROM control_docs WHERE collection=? AND key=?", (collection, key)).rowcount == 1

    def list(self, collection, *, status=None, scope=None, limit=None):
        with self._lock:
            sql = "SELECT key, doc, version FROM control_docs WHERE collection=?"
            params: list[Any] = [collection]
            if status is not None:
                sql += " AND status=?"
                params.append(status)
            if scope is not None:
                sql += " AND scope=?"
                params.append(scope)
            sql += " ORDER BY updated_at ASC, key ASC"
            if limit:
                sql += f" LIMIT {int(limit)}"
            return [(r[0], r[1], r[2]) for r in self._exec(sql, tuple(params)).fetchall()]

    def count(self, collection):
        with self._lock:
            return int(self._exec("SELECT COUNT(*) FROM control_docs WHERE collection=?", (collection,)).fetchone()[0])

    def append(self, collection, doc):
        with self._lock:
            if self.dialect == "postgres":
                row = self._exec("INSERT INTO control_log (collection, created_at, doc) VALUES (?,?,?) RETURNING id", (collection, self._ts(_now()), _dump(doc))).fetchone()
                return int(row[0])
            cur = self._exec("INSERT INTO control_log (collection, created_at, doc) VALUES (?,?,?)", (collection, self._ts(_now()), _dump(doc)))
            return int(cur.lastrowid)

    def log(self, collection: str) -> list[str]:
        with self._lock:
            return [r[0] for r in self._exec("SELECT doc FROM control_log WHERE collection=? ORDER BY id", (collection,)).fetchall()]

    def purge_older_than(self, collection, cutoff):
        with self._lock:
            return self._exec("DELETE FROM control_docs WHERE collection=? AND updated_at < ?", (collection, self._ts(cutoff))).rowcount

    def claim(self, collection, key, owner, lease_seconds, *, from_statuses):
        with self._lock:
            now = _now()
            placeholders = ",".join("?" for _ in from_statuses)
            cur = self._exec(
                f"UPDATE control_docs SET status='running', owner=?, lease_until=?, version=version+1, updated_at=? "
                f"WHERE collection=? AND key=? AND (status IN ({placeholders}) OR (status='running' AND lease_until IS NOT NULL AND lease_until < ?))",
                (owner, self._ts(now + timedelta(seconds=lease_seconds)), self._ts(now), collection, key, *from_statuses, self._ts(now)),
            )
            if cur.rowcount != 1:
                return None
            return self.get(collection, key)

    def status_of(self, collection: str, key: str) -> str | None:
        with self._lock:
            row = self._exec("SELECT status FROM control_docs WHERE collection=? AND key=?", (collection, key)).fetchone()
            return row[0] if row else None

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def control_store_from_url(url: str | None) -> ControlStore:
    """``None`` or ``memory://`` → in-memory; otherwise a SQL store."""
    if not url or url == "memory://":
        return InMemoryControlStore()
    return SqlControlStore(url)
