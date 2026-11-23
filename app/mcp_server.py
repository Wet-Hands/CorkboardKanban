"""Claude custom connector: a remote MCP server exposing Corkboard's task tools.

It runs in the same process as the web app (see `build_asgi_app`) and is
served at /mcp/<MCP_SECRET>. Add that full public URL in Claude under
Customize > Connectors > Add > Custom > Web.
"""

from __future__ import annotations

import functools
import logging
from typing import Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

import config
from app import models

log = logging.getLogger(__name__)

mcp = MCPServer(
    name="Corkboard",
    instructions=(
        "Corkboard is the user's physical kanban board: every task is printed as a card on a "
        "thermal printer and pinned to a corkboard. Tasks have a title, optional description, "
        "optional due date (YYYY-MM-DD), priority (low, medium, high) and status (todo, doing, done). "
        "Creating a task prints its card unless print_card is false. Use list_tasks to find task ids."
    ),
)

Priority = Literal["low", "medium", "high"]
Status = Literal["todo", "doing", "done"]
Recurrence = Literal["none", "daily", "weekdays", "weekly", "monthly"]


def _out(task: dict) -> dict:
    return {
        "id": task["id"],
        "title": task["title"],
        "description": task["description"],
        "due_date": task["due_date"],
        "due": models.due_state(task),
        "priority": task["priority"],
        "status": task["status"],
        "recurrence": task["recurrence"],
        "source": task["source"],
        "link": task["url"] or f"{config.BASE_URL}/task/{task['id']}",
        "last_printed_at": task["last_printed_at"],
    }


def _anticipated(fn):
    """Report bad input (unknown task, unreadable date...) to Claude as a readable tool error."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (ValueError, LookupError) as exc:
            raise ToolError(str(exc)) from exc

    return wrapper


def _get(task_id: int) -> dict:
    task = models.get_task(task_id)
    if task is None:
        raise ValueError(f"There is no task #{task_id}. Use list_tasks to see task ids.")
    return task


@mcp.tool(annotations=ToolAnnotations(title="Create task", destructive_hint=False))
@_anticipated
def create_task(
    title: str,
    description: str = "",
    due_date: str | None = None,
    priority: Priority = "medium",
    recurrence: Recurrence = "none",
    print_card: bool = True,
) -> dict:
    """Add a task to the board and (by default) print its card.

    due_date: YYYY-MM-DD, or "today", "tomorrow", a weekday name, or "+3d".
    recurrence: when the task is completed, a fresh card is created and printed on the next date.
    """
    task = models.create_task(
        title, description, due_date, priority, recurrence=recurrence, source="claude", print_card=print_card
    )
    return {**_out(task), "card_queued": print_card}


@mcp.tool(annotations=ToolAnnotations(title="List tasks", read_only_hint=True))
@_anticipated
def list_tasks(
    status: Literal["open", "todo", "doing", "done", "all"] = "open",
    priority: Priority | None = None,
    limit: int = 50,
) -> dict:
    """List tasks. "open" means To do and Doing; Done tasks are newest first."""
    limit = max(1, min(int(limit), 200))
    if status == "open":
        tasks = models.list_tasks(include_done=False, priority=priority, limit=limit)
    elif status == "all":
        tasks = models.list_tasks(priority=priority, limit=limit)
    else:
        tasks = models.list_tasks(status, priority=priority, limit=limit)
    return {"today": models.today().isoformat(), "count": len(tasks), "tasks": [_out(t) for t in tasks]}


@mcp.tool(annotations=ToolAnnotations(title="Update task", destructive_hint=False, idempotent_hint=True))
@_anticipated
def update_task(
    task_id: int,
    title: str | None = None,
    description: str | None = None,
    due_date: str | None = None,
    clear_due_date: bool = False,
    priority: Priority | None = None,
    status: Status | None = None,
    recurrence: Recurrence | None = None,
    reprint: bool = False,
) -> dict:
    """Change a task's fields; only the ones given are changed. Set reprint to print a fresh card."""
    _get(task_id)
    changes = {
        key: value
        for key, value in {
            "title": title, "description": description, "due_date": due_date,
            "priority": priority, "status": status, "recurrence": recurrence,
        }.items()
        if value is not None
    }
    if clear_due_date:
        changes["due_date"] = None
    task = models.update_task(task_id, **changes)
    if reprint:
        models.enqueue_print(task_id, "reprint")
    return {**_out(task), "card_queued": reprint}


@mcp.tool(annotations=ToolAnnotations(title="Complete task", destructive_hint=False, idempotent_hint=True))
@_anticipated
def complete_task(task_id: int) -> dict:
    """Mark a task as done."""
    _get(task_id)
    return _out(models.complete_task(task_id))


@mcp.tool(annotations=ToolAnnotations(title="Print task card", destructive_hint=False))
@_anticipated
def print_task(task_id: int) -> dict:
    """Print (or reprint) a task's card on the thermal printer."""
    task = _get(task_id)
    job = models.enqueue_print(task_id, "reprint")
    return {"queued": True, "job_id": job["id"], "task": _out(task)}


@mcp.tool(annotations=ToolAnnotations(title="Print daily digest", destructive_hint=False))
@_anticipated
def print_daily_digest() -> dict:
    """Print today's digest: overdue, due today, in progress and high-priority tasks."""
    data = models.digest()
    job = models.enqueue_digest()
    return {
        "queued": True,
        "job_id": job["id"],
        "overdue": len(data["overdue"]),
        "due_today": len(data["today"]),
        "in_progress": len(data["doing"]),
        "high_priority": len(data["high"]),
    }


def transport_security() -> TransportSecuritySettings:
    hosts = config.MCP_ALLOWED_HOSTS
    if not hosts:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[*hosts, "127.0.0.1:*", "localhost:*", "[::1]:*"],
        allowed_origins=["https://claude.ai", *(f"https://{h}" for h in hosts)],
    )


def build_asgi_app(flask_app):
    """One ASGI app: the MCP endpoint at /mcp/<secret>, and Flask for everything else."""
    from a2wsgi import WSGIMiddleware
    from starlette.applications import Starlette
    from starlette.routing import Mount

    routes = []
    lifespan = None
    if config.MCP_SECRET:
        mcp_app = mcp.streamable_http_app(
            streamable_http_path=f"/mcp/{config.MCP_SECRET}",
            stateless_http=True,
            json_response=True,
            transport_security=transport_security(),
        )
        routes.extend(mcp_app.routes)
        lifespan = mcp_app.router.lifespan_context
        log.info("Claude connector enabled at /mcp/<MCP_SECRET>")
    else:
        log.warning("MCP_SECRET is not set: the Claude connector is disabled")
    routes.append(Mount("/", app=WSGIMiddleware(flask_app)))
    return Starlette(routes=routes, lifespan=lifespan)
