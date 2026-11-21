"""One SQLite job queue for a local file or a shared Turso/libSQL database."""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, closing
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx

from nyc_gemini4_hack.models import ProjectState

type Statement = tuple[str, tuple[str | int, ...]]
type Rows = list[tuple]
type Execute = Callable[[list[Statement]], Awaitable[Rows]]


class JobStoreError(RuntimeError):
    pass


_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS craftpov_projects (
        id TEXT PRIMARY KEY, state TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
        worker TEXT, lease_until INTEGER, last_event TEXT, last_payload TEXT
    )""",
    """CREATE INDEX IF NOT EXISTS craftpov_queue ON craftpov_projects(worker, json_extract(state,'$.status'))""",
    """CREATE TABLE IF NOT EXISTS craftpov_events (
        project_id TEXT NOT NULL REFERENCES craftpov_projects(id) ON DELETE CASCADE,
        revision INTEGER NOT NULL, event TEXT NOT NULL, data TEXT NOT NULL,
        PRIMARY KEY(project_id, revision)
    )""",
    # Recording the event in the same UPDATE keeps the snapshot and its revision inseparable.
    """CREATE TRIGGER IF NOT EXISTS craftpov_record_event AFTER UPDATE OF revision ON craftpov_projects
       BEGIN
           INSERT INTO craftpov_events(project_id,revision,event,data)
               VALUES (NEW.id, NEW.revision, NEW.last_event, NEW.last_payload);
       END""",
    """CREATE TABLE IF NOT EXISTS craftpov_presence (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1), seen INTEGER NOT NULL
    )""",
]
_ANNOUNCE: Statement = ("""INSERT INTO craftpov_presence(singleton, seen) VALUES (1,unixepoch())
                           ON CONFLICT(singleton) DO UPDATE SET seen=excluded.seen RETURNING 1""", ())
_INTERRUPTED = "Generation was interrupted. Open the available steps or create a new guide to retry."


def _local_batch(path: Path, statements: list[Statement]) -> Rows:
    # Each operation owns a connection, including during cancellation of its async caller.
    with closing(sqlite3.connect(path, isolation_level=None, timeout=10)) as db:
        try:
            if len(statements) > 1:
                db.execute("BEGIN IMMEDIATE")
            for sql, args in statements:
                rows = db.execute(sql, args).fetchall()
            db.commit()
            return rows
        except sqlite3.Error as error:
            db.rollback()
            raise JobStoreError(str(error)) from error


async def _turso_batch(client: httpx.AsyncClient, statements: list[Statement]) -> Rows:
    # A single Hrana batch keeps every statement on one connection. Each step requires
    # the preceding step to succeed, including COMMIT. Closing rolls back a failed batch.
    transaction = len(statements) > 1
    batch = [("BEGIN IMMEDIATE", ()), *statements, ("COMMIT", ())] if transaction else statements
    steps = []
    for index, (sql, args) in enumerate(batch):
        values = [{"type": "integer" if isinstance(value, int) else "text", "value": str(value)} for value in args]
        step = {"stmt": {"sql": sql, "args": values, "want_rows": True}}
        if index:
            step["condition"] = {"type": "ok", "step": index - 1}
        steps.append(step)
    response = await client.post("v3/pipeline", json={"baton": None, "requests": [
        {"type": "batch", "batch": {"steps": steps}}, {"type": "close"},
    ]})
    response.raise_for_status()
    try:
        batch_response, close_response = response.json()["results"]
        for result in (batch_response, close_response):
            if result["type"] == "error":
                raise JobStoreError(result["error"]["message"])
        details = batch_response["response"]["result"]
        for error in details["step_errors"]:
            if error:
                raise JobStoreError(error["message"])
        rows = details["step_results"][-2 if transaction else -1]["rows"]
        # This store selects only integer and text columns.
        return [tuple(int(cell["value"]) if cell["type"] == "integer" else cell["value"]
                      for cell in row) for row in rows]
    except (TypeError, ValueError, KeyError, IndexError) as error:
        raise JobStoreError("Invalid libSQL response.") from error


class JobStore:
    def __init__(self, execute: Execute):
        self._execute = execute

    async def _run(self, *statements: Statement) -> Rows:
        try:
            return await self._execute(list(statements))
        except JobStoreError as error:
            if "no such table: craftpov_" not in str(error).lower():
                raise
        # Concurrent first-use operations can safely initialize the same empty database.
        # Normal reads do not take a write lock or require a schema round trip.
        await self._execute([(sql, ()) for sql in _SCHEMA])
        return await self._execute(list(statements))

    async def enqueue(self, state: ProjectState) -> None:
        await self._run(("INSERT INTO craftpov_projects (id,state) VALUES (?,?)",
                         (state.project_id, state.model_dump_json())))

    async def read(self, project_id: str) -> tuple[ProjectState, int] | None:
        rows = await self._run(("SELECT state,revision FROM craftpov_projects WHERE id=?", (project_id,)))
        if not rows:
            return None
        state, revision = rows[0]
        return ProjectState.model_validate_json(state), revision

    async def events_after(self, project_id: str, revision: int) -> list[dict]:
        rows = await self._run(("""SELECT revision,event,data FROM craftpov_events
                                   WHERE project_id=? AND revision>? ORDER BY revision LIMIT 128""",
                                (project_id, revision)))
        return [{"revision": index, "event": event, "data": json.loads(data)} for index, event, data in rows]

    async def online(self) -> bool:
        rows = await self._run(("SELECT 1 FROM craftpov_presence WHERE seen > unixepoch() - 90", ()))
        return bool(rows)

    async def heartbeat(self, worker_id: str, project_id: str | None = None) -> bool:
        if project_id is None:
            await self._run(_ANNOUNCE)
            return True
        rows = await self._run(_ANNOUNCE, ("""UPDATE craftpov_projects SET lease_until=unixepoch()+120
            WHERE id=? AND worker=? AND lease_until>unixepoch() RETURNING 1""", (project_id, worker_id)))
        return bool(rows)

    async def claim(self, worker_id: str) -> ProjectState | None:
        # Recover only abandoned work. Replaying a half-finished provider call can generate a second charge.
        expired_status = "CASE WHEN json_extract(state,'$.plan') IS NULL THEN 'failed' ELSE 'partial' END"
        recover = f"""UPDATE craftpov_projects SET revision=revision+1, worker=NULL, lease_until=NULL,
            state=json_set(state,'$.status',{expired_status},'$.status_message',?,'$.error',?),
            last_event='status', last_payload=json_object('status',{expired_status},'message',?)
            WHERE lease_until<=unixepoch()"""
        claim = """UPDATE craftpov_projects SET worker=?, lease_until=unixepoch()+120
            WHERE id=(SELECT id FROM craftpov_projects WHERE worker IS NULL AND json_extract(state,'$.status')='queued'
                      ORDER BY rowid LIMIT 1)
            RETURNING state"""
        rows = await self._run(_ANNOUNCE, (recover, (_INTERRUPTED,) * 3), (claim, (worker_id,)))
        return ProjectState.model_validate_json(rows[0][0]) if rows else None

    async def checkpoint(self, worker_id: str, project: ProjectState, event_name: str, payload: dict) -> bool:
        terminal = int(event_name == "status" and project.status.terminal)
        rows = await self._run(("""UPDATE craftpov_projects SET state=?, revision=revision+1, last_event=?, last_payload=?,
            worker=CASE WHEN ? THEN NULL ELSE worker END,
            lease_until=CASE WHEN ? THEN NULL ELSE unixepoch()+120 END
            WHERE id=? AND worker=? AND lease_until>unixepoch() RETURNING revision""",
            (project.model_dump_json(), event_name, json.dumps(payload), terminal, terminal, project.project_id, worker_id)))
        return bool(rows)


@asynccontextmanager
async def open_job_store(local_path: Path | None) -> AsyncIterator[JobStore]:
    if local_path is not None:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        yield JobStore(lambda statements: asyncio.to_thread(_local_batch, local_path, statements))
        return
    url = urlsplit(os.getenv("TURSO_DATABASE_URL", "").strip())
    token = os.getenv("TURSO_AUTH_TOKEN", "").strip()
    if not token or not url.hostname or url.scheme not in {"libsql", "https"}:
        raise JobStoreError("Set TURSO_DATABASE_URL and TURSO_AUTH_TOKEN for hosted guide storage.")
    endpoint = urlunsplit(("https", url.netloc, url.path.rstrip("/") + "/", "", ""))
    async with httpx.AsyncClient(base_url=endpoint, headers={"Authorization": f"Bearer {token}"}, timeout=20) as client:
        yield JobStore(lambda statements: _turso_batch(client, statements))
