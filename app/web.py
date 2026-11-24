"""Web UI: the board, task forms, card previews, reprints and the print queue."""

from __future__ import annotations

import hmac
import secrets
from datetime import date

from flask import (Blueprint, Flask, Response, abort, flash, redirect, render_template, request,
                   session, url_for)

import config
from app import models, printer, render

bp = Blueprint("web", __name__)
STATUS_LABELS = {"todo": "To do", "doing": "Doing", "done": "Done"}
OPEN_PATHS = ("/github", "/healthz", "/static/", "/mcp/")


# --- Auth and CSRF ---------------------------------------------------------------------

def _csrf_token() -> str:
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(24)
    return session["csrf"]


@bp.before_app_request
def protect():
    if request.path.startswith(OPEN_PATHS):
        return None
    if config.WEB_USERNAME and config.WEB_PASSWORD:
        auth = request.authorization
        ok = (
            auth is not None
            and hmac.compare_digest(auth.username or "", config.WEB_USERNAME)
            and hmac.compare_digest(auth.password or "", config.WEB_PASSWORD)
        )
        if not ok:
            return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="Corkboard"'})
    if request.method == "POST" and request.endpoint is not None:
        sent = request.form.get("csrf_token", "")
        if not sent or not hmac.compare_digest(sent, session.get("csrf", "")):
            abort(400, "Form expired; go back, reload the page and try again.")
    return None


def register_template_helpers(app: Flask) -> None:
    @app.context_processor
    def helpers():
        return {
            "csrf_token": _csrf_token,
            "status_labels": STATUS_LABELS,
            "due_state": models.due_state,
            "human_date": lambda iso: render.human_date(date.fromisoformat(iso)),
            "printer_status": printer.status(),
            "queue_counts": models.queue_counts(),
            "quiet": _quiet_state(),
            "checklist": models.checklist,
            "checklist_progress": models.checklist_progress,
        }


def _quiet_state() -> dict | None:
    until = models.quiet_until()
    if until is None:
        return None
    return {"until": f"{until:%H:%M}", "held": models.held_count(), "manual_ok": config.QUIET_HOURS_ALLOW_MANUAL}


# --- Helpers ----------------------------------------------------------------------------

def _task_or_404(task_id: int) -> dict:
    task = models.get_task(task_id)
    if task is None:
        abort(404)
    return task


def _form_fields() -> dict:
    form = request.form
    return {
        "title": form.get("title", ""),
        "description": form.get("description", ""),
        "due_date": form.get("due_date") or None,
        "priority": form.get("priority", "medium"),
        "recurrence": form.get("recurrence", "none"),
    }


def _back(default: str = "web.board"):
    target = request.form.get("next") or ""
    if target.startswith("/") and not target.startswith("//"):
        return redirect(target)
    return redirect(url_for(default))


def _png(img) -> Response:
    return Response(render.to_png(img), mimetype="image/png", headers={"Cache-Control": "no-store"})


# --- Board and tasks ----------------------------------------------------------------------

@bp.get("/")
def board():
    return render_template("board.html", columns=models.board())


@bp.route("/task/new", methods=["GET", "POST"])
def new_task():
    if request.method == "POST":
        fields = _form_fields()
        try:
            task = models.create_task(
                **fields,
                status=request.form.get("status", "todo"),
                print_card=bool(request.form.get("print_card")),
                manual=True,
            )
        except ValueError as exc:
            flash(str(exc), "error")
            return render_template("task_form.html", task=None, values=fields, print_default=True), 400
        flash(f"Added #{task['id']}" + (" and sent it to the printer." if request.form.get("print_card") else "."))
        return redirect(url_for("web.board"))
    return render_template(
        "task_form.html", task=None, values={"priority": "medium", "recurrence": "none"},
        print_default=config.PRINT_ON_CREATE,
    )


@bp.get("/task/<int:task_id>")
def task_detail(task_id: int):
    return render_template("task_detail.html", task=_task_or_404(task_id))


@bp.route("/task/<int:task_id>/edit", methods=["GET", "POST"])
def edit_task(task_id: int):
    task = _task_or_404(task_id)
    if request.method == "POST":
        fields = _form_fields()
        try:
            task = models.update_task(task_id, **fields)
        except ValueError as exc:
            flash(str(exc), "error")
            return render_template("task_form.html", task=task, values=fields, print_default=False), 400
        if request.form.get("print_card"):
            models.enqueue_print(task_id, "reprint", manual=True)
            flash(f"Saved #{task_id} and sent it to the printer.")
        else:
            flash(f"Saved #{task_id}.")
        return redirect(url_for("web.task_detail", task_id=task_id))
    return render_template("task_form.html", task=task, values=task, print_default=False)


@bp.post("/task/<int:task_id>/status")
def change_status(task_id: int):
    _task_or_404(task_id)
    try:
        task = models.set_status(task_id, request.form.get("status", ""))
    except ValueError as exc:
        flash(str(exc), "error")
    else:
        flash(f"#{task_id} moved to {STATUS_LABELS[task['status']]}.")
    return _back()


@bp.post("/task/<int:task_id>/print")
def print_task(task_id: int):
    _task_or_404(task_id)
    models.enqueue_print(task_id, "reprint", manual=True)
    flash(f"#{task_id} sent to the printer.")
    return _back()


@bp.post("/task/<int:task_id>/check/<int:line>")
def toggle_check(task_id: int, line: int):
    _task_or_404(task_id)
    try:
        models.toggle_checklist_item(task_id, line)
    except ValueError as exc:
        flash(str(exc), "error")
    return _back()


@bp.post("/task/<int:task_id>/delete")
def delete_task(task_id: int):
    _task_or_404(task_id)
    models.delete_task(task_id)
    flash(f"Deleted #{task_id}.")
    return redirect(url_for("web.board"))


@bp.get("/task/<int:task_id>/card.png")
def card_png(task_id: int):
    return _png(render.render_card(_task_or_404(task_id)))


@bp.get("/preview.png")
def preview_png():
    """Live preview for the task form; nothing is saved."""
    args = request.args
    try:
        due = models.parse_due_date(args.get("due_date"))
    except ValueError:
        due = None
    task = {
        "id": args.get("id") or "new",
        "title": (args.get("title") or "Untitled task")[: models.TITLE_MAX],
        "description": (args.get("description") or "")[: models.DESCRIPTION_MAX],
        "due_date": due,
        "priority": args.get("priority") if args.get("priority") in config.PRIORITIES else "medium",
        "status": "todo",
        "recurrence": args.get("recurrence") if args.get("recurrence") in config.RECURRENCES else "none",
        "source": args.get("source") or "web",
        "url": None,
    }
    return _png(render.render_card(task))


# --- Print queue and digest ----------------------------------------------------------------

@bp.get("/queue")
def queue():
    return render_template("queue.html", jobs=models.list_jobs(50))


@bp.post("/queue/<int:job_id>/retry")
def retry_job(job_id: int):
    models.retry_job(job_id)
    flash("Job back in the queue.")
    return redirect(url_for("web.queue"))


@bp.post("/queue/<int:job_id>/cancel")
def cancel_job(job_id: int):
    models.cancel_job(job_id)
    flash("Job removed.")
    return redirect(url_for("web.queue"))


@bp.post("/digest/print")
def print_digest():
    models.enqueue_digest(manual=True)
    flash("Digest sent to the printer.")
    return _back()


@bp.get("/digest.png")
def digest_png():
    return _png(render.render_digest(models.digest()))


@bp.get("/health")
def health_page():
    from app import health

    return render_template("health.html", h=health.gather())


@bp.get("/healthz")
def healthz():
    return {"ok": True, "printer": printer.status(), "queue": models.queue_counts()}
