"""Tasks cell API (contract tasks.v1).

Every request runs as the ``tasks_api`` database role, bound to the token's
workspace with ``SET LOCAL``. Writes follow one pattern:

1. a recorded, unlocked authorization check in its own transaction, so
   denials are audited even though nothing else happens;
2. one transaction that opens an execution, re-checks authorization with
   locks (``enforce``), changes the data, writes the outbox event and the
   audit event, and closes the execution.

Resources outside the caller's workspace or permissions answer 404, the
same as resources that do not exist.
"""

# No `from __future__ import annotations`: FastAPI resolves the dependency
# annotations declared inside create_app at runtime.
import hashlib
import json
import logging
import os
import re
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response
from fastapi import Request as HttpRequest
from fastapi import Path as Path_
from psycopg.rows import dict_row
from pydantic import BaseModel, ConfigDict, Field

from empire_authz import AuthorizationDenied, Request, Resource, authorize, enforce
from tasks_api.auth import Principal, TokenVerifier, Unauthenticated

Status = Literal["todo", "in_progress", "done", "blocked", "cancelled"]
Relation = Literal["supports-goal", "belongs-to", "related-to", "depends-on", "part-of"]
TargetType = Literal["goal", "project", "context", "skill", "task"]
TASK_COLUMNS = (
    "id", "title", "description", "status", "priority", "project", "tags", "category", "due_date",
    "completed_at", "progress_percent", "notes", "parent_task_id", "calendar_event_id", "started_at",
    "estimated_minutes", "actual_minutes", "reminder_at", "reminder_channels", "reminder_sent",
    "recurrence_rule", "is_recurring_instance", "recurrence_occurrence", "parent_recurring_id",
    "external_source", "external_id", "external_url", "archived_at", "archive_reason",
    "version", "created_at", "updated_at", "legacy_id",
)
# PATCH fields that narrower automation permissions may change.
STATUS_FIELDS = {"status", "due_date", "progress_percent"}
CALENDAR_FIELDS = {"calendar_event_id"}
CORRELATION = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
correlation_id: ContextVar[str] = ContextVar("correlation_id", default="")
log = logging.getLogger("tasks_api")


class TaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=500)
    description: str | None = None
    status: Status = "todo"
    priority: int = Field(default=3, ge=1, le=5)
    project: str | None = None
    tags: list[str] = Field(default_factory=list)
    category: str | None = None
    due_date: date | None = None
    notes: str | None = None
    parent_task_id: uuid.UUID | None = None
    calendar_event_id: str | None = None
    started_at: datetime | None = None
    estimated_minutes: int | None = Field(default=None, ge=0)
    actual_minutes: int = Field(default=0, ge=0)
    reminder_at: datetime | None = None
    reminder_channels: str | None = None
    recurrence_rule: str | None = None
    external_url: str | None = None


class TaskUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, min_length=1, max_length=500)
    description: str | None = None
    status: Literal["todo", "in_progress", "blocked", "cancelled"] | None = None
    priority: int | None = Field(default=None, ge=1, le=5)
    project: str | None = None
    tags: list[str] | None = None
    category: str | None = None
    due_date: date | None = None
    progress_percent: int | None = Field(default=None, ge=0, le=100)
    notes: str | None = None
    parent_task_id: uuid.UUID | None = None
    calendar_event_id: str | None = None
    started_at: datetime | None = None
    estimated_minutes: int | None = Field(default=None, ge=0)
    actual_minutes: int | None = Field(default=None, ge=0)
    reminder_at: datetime | None = None
    reminder_channels: str | None = None
    recurrence_rule: str | None = None
    external_url: str | None = None


class Archive(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=500)


class ReminderDelivered(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reminder_at: datetime
    channel: str = Field(pattern=r"^[a-z][a-z0-9-]{0,31}$")


class GenerateOccurrences(BaseModel):
    model_config = ConfigDict(extra="forbid")
    until: date


class UpsertResult(BaseModel):
    outcome: Literal["created", "updated", "unchanged"]
    task: "Task"


class ReminderResult(BaseModel):
    outcome: Literal["recorded", "already_recorded"]


class OccurrenceResult(BaseModel):
    created: list["Task"]
    existing: int


class LinkCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    relation: Relation
    target_type: TargetType
    target_ref: str = Field(pattern=r"^empire:[a-z0-9-]+:[a-z0-9-]+:[A-Za-z0-9:._-]+$")


class Task(BaseModel):
    id: uuid.UUID
    urn: str
    number: int
    title: str
    description: str | None
    status: Status
    priority: int
    project: str | None
    tags: list[str]
    category: str | None
    due_date: date | None
    completed_at: datetime | None
    progress_percent: int
    notes: str | None
    parent_task_id: uuid.UUID | None
    calendar_event_id: str | None
    started_at: datetime | None
    estimated_minutes: int | None
    actual_minutes: int
    reminder_at: datetime | None
    reminder_channels: str | None
    reminder_sent: bool
    recurrence_rule: str | None
    is_recurring_instance: bool
    recurrence_occurrence: int | None
    parent_recurring_id: uuid.UUID | None
    external_source: str | None
    external_id: str | None
    external_url: str | None
    archived_at: datetime | None
    archive_reason: str | None
    version: int
    created_at: datetime
    updated_at: datetime


class TaskList(BaseModel):
    items: list[Task]


class Link(BaseModel):
    id: uuid.UUID
    task_id: uuid.UUID
    relation: Relation
    target_type: TargetType
    target_ref: str


class Problem(BaseModel):
    error: str
    reason: str | None = None


def urn(task_id: uuid.UUID) -> str:
    return f"empire:tasks:task:{task_id}"


def to_task(row: dict[str, Any]) -> Task:
    return Task(urn=urn(row["id"]), number=row["legacy_id"], **{k: row[k] for k in TASK_COLUMNS if k != "legacy_id"})


def audit_log(**fields: Any) -> None:
    """One structured line per write decision; IDs and codes only, never content."""
    log.info(json.dumps({"correlation_id": correlation_id.get(), **fields}, default=str, sort_keys=True))


def recurrence_step(rule: str | None) -> timedelta | None:
    """Same rule language as the SQLite-era recurring_gen.py."""
    if not rule:
        return None
    rule = rule.strip().lower()
    fixed = {"daily": 1, "every day": 1, "weekly": 7, "every week": 7, "monthly": 30, "every month": 30,
             "biweekly": 14, "every 2 weeks": 14, "every 3 months": 90}
    if rule in fixed:
        return timedelta(days=fixed[rule])
    for pattern, days in ((r"every\s+(\d+)\s*months?", 30), (r"every\s+(\d+)\s*d", 1), (r"every\s+(\d+)\s*w", 7)):
        match = re.match(pattern, rule)
        if match:
            return timedelta(days=int(match.group(1)) * days)
    return None


def next_occurrence(parent: dict[str, Any], existing: list[dict[str, Any]], step: timedelta,
                    until: date) -> tuple[int, date]:
    """Highest occurrence so far and the next due date (recurring_gen.py semantics)."""
    if existing:
        latest = existing[-1]
        anchor = latest["due_date"] or (latest["completed_at"].date() if latest["completed_at"] else None)
        if anchor is not None:
            return latest["recurrence_occurrence"], anchor + step
        return latest["recurrence_occurrence"], until
    if parent["due_date"]:
        return 0, parent["due_date"] + step
    return 0, until


def row_hash(row: dict[str, Any] | None) -> str | None:
    if row is None:
        return None
    return hashlib.sha256(json.dumps(row, sort_keys=True, default=str).encode()).hexdigest()


def create_app(
    connect: Callable[[], psycopg.Connection],
    verifier: TokenVerifier,
) -> FastAPI:
    app = FastAPI(title="Tasks API", version="1.0.0", description=__doc__)

    @app.middleware("http")
    async def correlate(request: HttpRequest, call_next):
        """Carry X-Correlation-Id through the execution, outbox event, and logs."""
        given = request.headers.get("x-correlation-id", "")
        token = correlation_id.set(given if CORRELATION.match(given) else uuid.uuid4().hex)
        try:
            response = await call_next(request)
        finally:
            value = correlation_id.get()
            correlation_id.reset(token)
        response.headers["X-Correlation-Id"] = value
        return response

    def principal(
        authorization: Annotated[str | None, Header()] = None,
        x_empire_channel: Annotated[str | None, Header()] = None,
    ) -> Principal:
        try:
            return verifier.verify(authorization, x_empire_channel)
        except Unauthenticated as exc:
            raise HTTPException(status_code=401, detail={"error": "unauthenticated", "reason": str(exc)}) from exc

    Caller = Annotated[Principal, Depends(principal)]

    @contextmanager
    def bound(caller: Principal) -> Iterator[psycopg.Connection]:
        try:
            conn = connect()
        except psycopg.OperationalError as exc:  # pool or server exhausted: fail fast, do not hang
            raise HTTPException(status_code=503, detail={"error": "unavailable"}) from exc
        conn.row_factory = dict_row
        try:
            conn.execute("SELECT set_config('app.workspace_id', %s, true)", (str(caller.workspace),))
            yield conn
        finally:
            conn.rollback()
            conn.close()

    def request_for(caller: Principal, action: str, resource_id: str, approval: uuid.UUID | None = None) -> Request:
        return Request(
            actor=caller.actor, workspace=caller.workspace, action=action,
            resource=Resource("task", resource_id, caller.workspace), channel=caller.channel,
            delegation=caller.delegation,
            on_behalf_of=caller.on_behalf_of if caller.delegation else None,
            approval=approval,
        )

    def precheck(caller: Principal, request: Request, *, not_found: bool) -> None:
        """Recorded, unlocked decision in its own transaction."""
        if caller.scopes is not None and request.action not in caller.scopes:
            if not_found:
                raise HTTPException(status_code=404, detail={"error": "not_found"})
            raise HTTPException(status_code=403, detail={"error": "deny", "reason": "scope_insufficient"})
        with bound(caller) as conn:
            decision = authorize(conn, request)
            conn.commit()
        if decision.allowed:
            return
        if not_found:
            raise HTTPException(status_code=404, detail={"error": "not_found"})
        status = 409 if decision.decision == "require_approval" else 403
        raise HTTPException(status_code=status, detail={"error": decision.decision, "reason": decision.reason})

    def load(conn: psycopg.Connection, task_id: uuid.UUID, *, lock: bool = False) -> dict[str, Any]:
        row = conn.execute(
            f"SELECT {', '.join(TASK_COLUMNS)} FROM tasks.tasks WHERE id = %s{' FOR UPDATE' if lock else ''}",
            (task_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail={"error": "not_found"})
        return row

    @contextmanager
    def acting(caller: Principal, request: Request, idempotency_key: str | None) -> Iterator[tuple[psycopg.Connection, uuid.UUID]]:
        """Execution + locked re-check + the write, all in one transaction."""
        with bound(caller) as conn:
            # Locks the switch row: switching writes off waits for writes already under way.
            if not conn.execute("SELECT tasks.writes_enabled() AS on").fetchone()["on"]:
                raise HTTPException(status_code=503, detail={"error": "writes_disabled"})
            try:
                # The execution trigger refuses acting for someone without a live delegation.
                execution = conn.execute(
                    "SELECT platform.begin_execution(%s, %s, %s, %s, %s, %s, %s, %s) AS id",
                    (caller.initiated_by, caller.actor, caller.on_behalf_of, caller.delegation,
                     request.approval, caller.channel, idempotency_key, correlation_id.get()),
                ).fetchone()["id"]
                enforce(conn, request)
            except (AuthorizationDenied, psycopg.errors.InsufficientPrivilege) as denied:
                conn.rollback()
                with bound(caller) as audit:
                    # Record the denial outside the rolled-back transaction.
                    decision = authorize(audit, request)
                    audit.commit()
                if isinstance(denied, AuthorizationDenied):
                    decision = denied.decision
                elif decision.allowed:  # the trigger refused although the PDP allows: fail closed
                    decision = type(decision)("deny", "delegation_invalid", decision.decision_id)
                status = 409 if decision.decision == "require_approval" else 403
                audit_log(event="tasks.write.denied", action=request.action, reason=decision.reason,
                          actor=caller.actor, workspace=caller.workspace)
                raise HTTPException(status_code=status, detail={"error": decision.decision,
                                                                "reason": decision.reason}) from denied
            try:
                yield conn, execution
            except psycopg.errors.ForeignKeyViolation as exc:
                raise HTTPException(status_code=422, detail={"error": "reference_not_found"}) from exc
            except psycopg.errors.UniqueViolation as exc:
                raise HTTPException(status_code=409, detail={"error": "conflict"}) from exc
            conn.execute("SELECT platform.finish_execution(%s, 'succeeded')", (execution,))
            conn.commit()
            audit_log(event="tasks.write", action=request.action, execution=execution,
                      actor=caller.actor, workspace=caller.workspace)

    def emit(conn: psycopg.Connection, caller: Principal, execution: uuid.UUID, event: str,
             task_id: uuid.UUID, before: dict | None, after: dict | None, extra: dict | None = None) -> None:
        """Outbox event and audit record in the same transaction as the change."""
        current = after or before or {}
        payload: dict[str, Any] = {
            "task": urn(task_id),
            # A delete bumps the version past the last state so consumers can order it.
            "version": current.get("version", 0) + (1 if after is None else 0),
            "deleted": after is None,
        }
        if after is not None:
            payload.update(title=after["title"], status=after["status"], archived=after["archived_at"] is not None)
        payload.update(extra or {})
        event_type = event if "." in event else f"tasks.task.{event}"
        conn.execute(
            "INSERT INTO tasks.outbox (workspace_id, type, subject, execution_id, payload, correlation_id)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            (caller.workspace, event_type, urn(task_id), execution, json.dumps(payload, default=str),
             correlation_id.get()),
        )
        conn.execute(
            "SELECT platform.record_audit(%s, 'tasks', %s, 'task', %s, %s, %s)",
            (execution, event_type, str(task_id), row_hash(before), row_hash(after)),
        )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/tasks", response_model=TaskList, responses={401: {"model": Problem}, 403: {"model": Problem}})
    def list_tasks(
        caller: Caller,
        status: Annotated[list[Status] | None, Query()] = None,
        external_source: Annotated[str | None, Query(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")] = None,
        due_before: date | None = None,
        reminder_due_before: datetime | None = None,
        missing_calendar_event: bool = False,
        include_archived: bool = False,
        after: Annotated[int | None, Query(ge=0, description="Cursor: return tasks numbered above this")] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> TaskList:
        """Filters cover what the SQLite-era writers selected; results are ordered by task number."""
        precheck(caller, request_for(caller, "tasks.task:read", "*"), not_found=False)
        with bound(caller) as conn:
            rows = conn.execute(
                f"SELECT {', '.join(TASK_COLUMNS)} FROM tasks.tasks"
                " WHERE (%(archived)s OR archived_at IS NULL)"
                " AND (%(status)s::text[] IS NULL OR status = ANY(%(status)s))"
                " AND (%(source)s::text IS NULL OR external_source = %(source)s)"
                " AND (%(due)s::date IS NULL OR due_date < %(due)s)"
                " AND (%(reminder)s::timestamptz IS NULL OR (reminder_at <= %(reminder)s AND NOT reminder_sent))"
                " AND (NOT %(calendar)s OR calendar_event_id IS NULL)"
                " AND (%(after)s::bigint IS NULL OR legacy_id > %(after)s)"
                " ORDER BY legacy_id LIMIT %(limit)s",
                {"archived": include_archived, "status": status, "source": external_source, "due": due_before,
                 "reminder": reminder_due_before, "calendar": missing_calendar_event, "after": after, "limit": limit},
            ).fetchall()
        return TaskList(items=[to_task(row) for row in rows])

    @app.get("/v1/tasks/number/{number}", response_model=Task,
             responses={401: {"model": Problem}, 404: {"model": Problem}})
    def get_by_number(caller: Caller, number: int) -> Task:
        with bound(caller) as conn:
            row = conn.execute(f"SELECT {', '.join(TASK_COLUMNS)} FROM tasks.tasks WHERE legacy_id = %s",
                               (number,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail={"error": "not_found"})
        precheck(caller, request_for(caller, "tasks.task:read", str(row["id"])), not_found=True)
        return to_task(row)

    @app.get("/v1/tasks/external/{source}/{external_id}", response_model=Task,
             responses={401: {"model": Problem}, 404: {"model": Problem}})
    def get_external(caller: Caller, source: Annotated[str, Path_(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")],
                     external_id: Annotated[str, Path_(min_length=1, max_length=200)]) -> Task:
        with bound(caller) as conn:
            row = conn.execute(f"SELECT {', '.join(TASK_COLUMNS)} FROM tasks.tasks"
                               " WHERE external_source = %s AND external_id = %s", (source, external_id)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail={"error": "not_found"})
        precheck(caller, request_for(caller, "tasks.task:read", str(row["id"])), not_found=True)
        return to_task(row)

    @app.post("/v1/tasks", status_code=201, response_model=Task,
              responses={401: {"model": Problem}, 403: {"model": Problem}})
    def create_task(caller: Caller, body: TaskCreate, response: Response,
                    idempotency_key: Annotated[str | None, Header()] = None) -> Task:
        request = request_for(caller, "tasks.task:write", "*")
        precheck(caller, request, not_found=False)
        if idempotency_key:
            with bound(caller) as conn:
                previous = conn.execute("SELECT * FROM platform.find_execution(%s)", (idempotency_key,)).fetchone()
                if previous and previous["status"] == "succeeded":
                    row = conn.execute(
                        f"SELECT {', '.join(TASK_COLUMNS)} FROM tasks.tasks WHERE created_by_execution = %s",
                        (previous["id"],),
                    ).fetchone()
                    if row:
                        response.status_code = 200
                        return to_task(row)
        with acting(caller, request, idempotency_key) as (conn, execution):
            values = body.model_dump()
            if values["status"] == "done":
                values["completed_at"] = datetime.now().astimezone()
            values.update(workspace_id=caller.workspace, created_by_execution=execution, updated_by_execution=execution)
            columns = list(values)
            row = conn.execute(
                f"INSERT INTO tasks.tasks ({', '.join(columns)}) VALUES ({', '.join(['%s'] * len(columns))})"
                f" RETURNING {', '.join(TASK_COLUMNS)}",
                [values[c] for c in columns],
            ).fetchone()
            emit(conn, caller, execution, "created", row["id"], None, row)
        return to_task(row)

    @app.get("/v1/tasks/{task_id}", response_model=Task, responses={401: {"model": Problem}, 404: {"model": Problem}})
    def get_task(caller: Caller, task_id: uuid.UUID) -> Task:
        precheck(caller, request_for(caller, "tasks.task:read", str(task_id)), not_found=True)
        with bound(caller) as conn:
            return to_task(load(conn, task_id))

    def authorized_for(caller: Principal, task_id: uuid.UUID, action: str,
                       approval: uuid.UUID | None = None) -> Request:
        """404 unless the task exists and is readable; then the action's precheck."""
        request = request_for(caller, action, str(task_id), approval)
        with bound(caller) as conn:
            load(conn, task_id)
        # Callers that may not read the task learn nothing about it.
        precheck(caller, request_for(caller, "tasks.task:read", str(task_id)), not_found=True)
        precheck(caller, request, not_found=False)
        return request

    def change(caller: Principal, task_id: uuid.UUID, action: str, event: str,
               apply: Callable[[psycopg.Connection, dict[str, Any], uuid.UUID], dict[str, Any] | None],
               approval: uuid.UUID | None = None, idempotency_key: str | None = None,
               extra: dict | None = None) -> dict[str, Any] | None:
        request = authorized_for(caller, task_id, action, approval)
        with acting(caller, request, idempotency_key) as (conn, execution):
            before = load(conn, task_id, lock=True)
            after = apply(conn, before, execution)
            emit(conn, caller, execution, event, task_id, before, after, extra)
        return after

    @app.patch("/v1/tasks/{task_id}", response_model=Task,
               responses={401: {"model": Problem}, 403: {"model": Problem}, 404: {"model": Problem},
                          412: {"model": Problem}})
    def update_task(caller: Caller, task_id: uuid.UUID, body: TaskUpdate,
                    if_match: Annotated[str | None, Header()] = None) -> Task:
        updates = body.model_dump(exclude_unset=True)
        if not updates:
            raise HTTPException(status_code=422, detail={"error": "empty_update"})

        def apply(conn, before, execution):
            if if_match is not None and str(before["version"]) != if_match.strip('"'):
                raise HTTPException(status_code=412, detail={"error": "version_mismatch"})
            assignments = ", ".join(f"{column} = %s" for column in updates)
            return conn.execute(
                f"UPDATE tasks.tasks SET {assignments}, updated_by_execution = %s WHERE id = %s"
                f" RETURNING {', '.join(TASK_COLUMNS)}",
                [*updates.values(), execution, task_id],
            ).fetchone()

        if set(updates) <= STATUS_FIELDS:
            action = "tasks.status:write"
        elif set(updates) <= CALENDAR_FIELDS:
            action = "tasks.calendar:write"
        else:
            action = "tasks.task:write"
        return to_task(change(caller, task_id, action, "updated", apply))

    @app.post("/v1/tasks/{task_id}/complete", response_model=Task,
              responses={401: {"model": Problem}, 403: {"model": Problem}, 404: {"model": Problem}})
    def complete_task(caller: Caller, task_id: uuid.UUID) -> Task:
        def apply(conn, before, execution):
            return conn.execute(
                "UPDATE tasks.tasks SET status = 'done', completed_at = coalesce(completed_at, now()),"
                " progress_percent = 100, updated_by_execution = %s WHERE id = %s"
                f" RETURNING {', '.join(TASK_COLUMNS)}",
                (execution, task_id),
            ).fetchone()

        return to_task(change(caller, task_id, "tasks.status:write", "completed", apply))

    @app.delete("/v1/tasks/{task_id}", status_code=204,
                responses={401: {"model": Problem}, 403: {"model": Problem}, 404: {"model": Problem},
                           409: {"model": Problem}})
    def delete_task(caller: Caller, task_id: uuid.UUID,
                    x_empire_approval: Annotated[uuid.UUID | None, Header()] = None) -> Response:
        def apply(conn, before, execution):
            conn.execute("DELETE FROM tasks.tasks WHERE id = %s", (task_id,))
            return None

        change(caller, task_id, "tasks.task:delete", "deleted", apply, approval=x_empire_approval)
        return Response(status_code=204)

    @app.post("/v1/tasks/{task_id}/links", status_code=201, response_model=Link,
              responses={401: {"model": Problem}, 403: {"model": Problem}, 404: {"model": Problem}})
    def link_task(caller: Caller, task_id: uuid.UUID, body: LinkCreate) -> Link:
        created: dict[str, Any] = {}

        def apply(conn, before, execution):
            row = conn.execute(
                "INSERT INTO tasks.task_links (workspace_id, task_id, relation, target_type, target_ref)"
                " VALUES (%s, %s, %s, %s, %s)"
                " ON CONFLICT (task_id, relation, target_ref) DO UPDATE SET updated_at = now()"
                " RETURNING id, task_id, relation, target_type, target_ref",
                (caller.workspace, task_id, body.relation, body.target_type, body.target_ref),
            ).fetchone()
            created.update(row)
            return before

        change(caller, task_id, "tasks.link:write", "linked", apply,
               extra={"relation": body.relation, "target_type": body.target_type, "target_ref": body.target_ref})
        return Link(**created)

    @app.put("/v1/tasks/external/{source}/{external_id}", response_model=UpsertResult,
             responses={401: {"model": Problem}, 403: {"model": Problem}, 422: {"model": Problem}})
    def upsert_external(caller: Caller, source: Annotated[str, Path_(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")],
                        external_id: Annotated[str, Path_(min_length=1, max_length=200)],
                        body: TaskCreate, response: Response) -> UpsertResult:
        """Create or update the one task for an external record; retries never duplicate it."""
        request = request_for(caller, "tasks.task:write", f"external:{source}:{external_id}")
        precheck(caller, request, not_found=False)
        given = body.model_dump(exclude_unset=True)
        with acting(caller, request, None) as (conn, execution):
            values = body.model_dump()
            if values["status"] == "done":
                values["completed_at"] = datetime.now().astimezone()
            values.update(workspace_id=caller.workspace, external_source=source, external_id=external_id,
                          created_by_execution=execution, updated_by_execution=execution)
            columns = list(values)
            row = conn.execute(
                f"INSERT INTO tasks.tasks ({', '.join(columns)}) VALUES ({', '.join(['%s'] * len(columns))})"
                " ON CONFLICT (workspace_id, external_source, external_id)"
                " WHERE external_source IS NOT NULL AND external_id IS NOT NULL DO NOTHING"
                f" RETURNING {', '.join(TASK_COLUMNS)}",
                [values[c] for c in columns],
            ).fetchone()
            if row is not None:
                emit(conn, caller, execution, "created", row["id"], None, row)
                response.status_code = 201
                return UpsertResult(outcome="created", task=to_task(row))
            before = conn.execute(
                f"SELECT {', '.join(TASK_COLUMNS)} FROM tasks.tasks"
                " WHERE external_source = %s AND external_id = %s FOR UPDATE",
                (source, external_id),
            ).fetchone()
            changes = {k: v for k, v in given.items() if before.get(k) != v}
            if not changes:
                return UpsertResult(outcome="unchanged", task=to_task(before))
            if changes.get("status") == "done":
                changes["completed_at"] = before["completed_at"] or datetime.now().astimezone()
            assignments = ", ".join(f"{column} = %s" for column in changes)
            after = conn.execute(
                f"UPDATE tasks.tasks SET {assignments}, updated_by_execution = %s WHERE id = %s"
                f" RETURNING {', '.join(TASK_COLUMNS)}",
                [*changes.values(), execution, before["id"]],
            ).fetchone()
            emit(conn, caller, execution, "updated", after["id"], before, after)
            return UpsertResult(outcome="updated", task=to_task(after))

    @app.post("/v1/tasks/{task_id}/archive", response_model=Task,
              responses={401: {"model": Problem}, 403: {"model": Problem}, 404: {"model": Problem}})
    def archive_task(caller: Caller, task_id: uuid.UUID, body: Archive) -> Task:
        def apply(conn, before, execution):
            return conn.execute(
                "UPDATE tasks.tasks SET archived_at = coalesce(archived_at, now()), archive_reason = %s,"
                f" updated_by_execution = %s WHERE id = %s RETURNING {', '.join(TASK_COLUMNS)}",
                (body.reason, execution, task_id),
            ).fetchone()

        return to_task(change(caller, task_id, "tasks.archive:write", "archived", apply))

    @app.post("/v1/tasks/{task_id}/unarchive", response_model=Task,
              responses={401: {"model": Problem}, 403: {"model": Problem}, 404: {"model": Problem}})
    def unarchive_task(caller: Caller, task_id: uuid.UUID) -> Task:
        """Recovery path of the deletion policy: archived tasks come back intact."""
        def apply(conn, before, execution):
            return conn.execute(
                "UPDATE tasks.tasks SET archived_at = NULL, archive_reason = NULL, updated_by_execution = %s"
                f" WHERE id = %s RETURNING {', '.join(TASK_COLUMNS)}",
                (execution, task_id),
            ).fetchone()

        return to_task(change(caller, task_id, "tasks.archive:write", "unarchived", apply))

    @app.post("/v1/tasks/{task_id}/reminders/delivered", response_model=ReminderResult,
              responses={401: {"model": Problem}, 403: {"model": Problem}, 404: {"model": Problem}})
    def reminder_delivered(caller: Caller, task_id: uuid.UUID, body: ReminderDelivered) -> ReminderResult:
        """Record one delivery per reminder time and channel; notifier retries are no-ops."""
        request = authorized_for(caller, task_id, "tasks.reminder:write")
        with acting(caller, request, None) as (conn, execution):
            before = load(conn, task_id, lock=True)
            delivered = conn.execute(
                "INSERT INTO tasks.reminder_deliveries (workspace_id, task_id, reminder_at, channel, execution_id)"
                " VALUES (%s, %s, %s, %s, %s) ON CONFLICT (task_id, reminder_at, channel) DO NOTHING RETURNING id",
                (caller.workspace, task_id, body.reminder_at, body.channel, execution),
            ).fetchone()
            if delivered is None:
                return ReminderResult(outcome="already_recorded")
            after = conn.execute(
                "UPDATE tasks.tasks SET reminder_sent = true, updated_by_execution = %s"
                f" WHERE id = %s AND reminder_at = %s RETURNING {', '.join(TASK_COLUMNS)}",
                (execution, task_id, body.reminder_at),
            ).fetchone() or before
            emit(conn, caller, execution, "tasks.reminder.delivered", task_id, before, after,
                 {"reminder_at": body.reminder_at.isoformat(), "channel": body.channel})
            return ReminderResult(outcome="recorded")

    @app.post("/v1/tasks/{task_id}/occurrences/generate", response_model=OccurrenceResult,
              responses={401: {"model": Problem}, 403: {"model": Problem}, 404: {"model": Problem},
                         422: {"model": Problem}})
    def generate_occurrences(caller: Caller, task_id: uuid.UUID, body: GenerateOccurrences) -> OccurrenceResult:
        """Create the due instances of a recurring task up to ``until``; retries create nothing new."""
        request = authorized_for(caller, task_id, "tasks.occurrence:write")
        with acting(caller, request, None) as (conn, execution):
            parent = load(conn, task_id, lock=True)
            step = recurrence_step(parent["recurrence_rule"])
            if step is None:
                raise HTTPException(status_code=422, detail={"error": "no_recurrence_rule"})
            existing = conn.execute(
                "SELECT recurrence_occurrence, due_date, completed_at FROM tasks.tasks"
                " WHERE parent_recurring_id = %s ORDER BY recurrence_occurrence",
                (task_id,),
            ).fetchall()
            occurrence, next_due = next_occurrence(parent, existing, step, body.until)
            created = []
            while next_due <= body.until:
                occurrence += 1
                row = conn.execute(
                    "INSERT INTO tasks.tasks (workspace_id, title, description, priority, project, tags, category,"
                    " due_date, recurrence_rule, estimated_minutes, is_recurring_instance, recurrence_occurrence,"
                    " parent_recurring_id, created_by_execution, updated_by_execution)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, true, %s, %s, %s, %s)"
                    " ON CONFLICT (parent_recurring_id, recurrence_occurrence) WHERE parent_recurring_id IS NOT NULL"
                    f" DO NOTHING RETURNING {', '.join(TASK_COLUMNS)}",
                    (caller.workspace, f"[R{occurrence}] {parent['title']}", parent["description"],
                     parent["priority"], parent["project"], parent["tags"], parent["category"], next_due,
                     parent["recurrence_rule"], parent["estimated_minutes"], occurrence, task_id, execution, execution),
                ).fetchone()
                if row is not None:
                    emit(conn, caller, execution, "created", row["id"], None, row)
                    created.append(to_task(row))
                next_due = next_due + step
            return OccurrenceResult(created=created, existing=len(existing))

    return app


def main() -> None:
    import uvicorn

    from empire_platform.devdb import default_cluster

    key_path = Path(os.environ.get("TASKS_API_JWT_PUBLIC_KEY", ".runtime/dev-keys/public.pem"))
    verifier = TokenVerifier(key_path.read_text(encoding="utf-8"),
                             os.environ.get("TASKS_API_JWT_ISSUER", "empire-platform-dev"))
    cluster = default_cluster()
    app = create_app(lambda: cluster.connect("tasks_api"), verifier)
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("TASKS_API_PORT", "3601")))
