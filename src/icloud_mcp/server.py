"""The iCloud MCP server.

Read-only v1: tools and resources that list/search/read mail and calendar.
Synchronous IMAP/CalDAV calls are dispatched to a thread pool so they never
block the asyncio event loop.
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import asynccontextmanager, nullcontext
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import BaseModel

from . import calendar as cal_ops
from . import contacts as contacts_ops
from . import mail as mail_ops
from . import writes as write_ops
from .clients import CalendarClient, ContactsClient, MailClient
from .config import Settings
from .models import Calendar, Contact, Event, Folder, FullMessage, Message

log = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="icloud-io")
_CALL_TIMEOUT = 30  # seconds, reads
# Writes get longer than one SMTP send's worst case (connect, STARTTLS, login and
# send at up to 20s each), so a slow but healthy send reports its real outcome.
_WRITE_TIMEOUT = 120  # seconds


def _call_locked(fn, *args):
    """Run `fn` holding its client's lock (first arg), if it has one.

    The shared IMAP/DAV connections aren't thread-safe, so the lock spans the
    whole operation. SMTP sends take `Settings` and open their own connection.
    """
    lock = getattr(args[0], "lock", None) if args else None
    with lock if lock is not None else nullcontext():
        return fn(*args)


def _submit(fn, *args) -> Future:
    return _executor.submit(_call_locked, fn, *args)


async def _run_blocking(fn, *args):
    return await asyncio.wait_for(asyncio.wrap_future(_submit(fn, *args)), _CALL_TIMEOUT)


def _log_late_outcome(name: str):
    """Log how a timed-out write eventually ended, since no caller is waiting."""

    def done(fut: Future) -> None:
        exc = fut.exception()
        if exc is not None:
            log.error("%s finished after timing out, and FAILED: %s", name, exc)
        else:
            log.warning("%s finished after timing out, and succeeded: %s", name, fut.result())

    return done


async def _run_write(fn, *args) -> str:
    """Run a mutating op. On timeout, say whether it might still go through.

    wait_for stops waiting but can't stop a worker thread that has started, so a
    timed-out send or create may still happen. The error says so, to discourage a
    retry that would duplicate it. A write that never left the queue is cancelled
    and is safe to retry.
    """
    fut = _submit(fn, *args)
    try:
        return await asyncio.wait_for(asyncio.wrap_future(fut), _WRITE_TIMEOUT)
    except TimeoutError:
        if fut.cancel():
            raise ToolError(
                f"Timed out after {_WRITE_TIMEOUT}s waiting to start; nothing was "
                "changed. Safe to retry."
            ) from None
        fut.add_done_callback(_log_late_outcome(getattr(fn, "__name__", "write")))
        raise ToolError(
            f"Timed out after {_WRITE_TIMEOUT}s, but the action may still complete. "
            "Do not retry: check Sent mail, the calendar, or contacts first to see "
            "whether it went through."
        ) from None


@dataclass
class AppContext:
    settings: Settings
    mail: MailClient
    calendar: CalendarClient
    contacts: ContactsClient


@asynccontextmanager
async def lifespan(_server: FastMCP):
    settings = Settings.from_env()
    mail = MailClient(settings)
    calendar = CalendarClient(settings)
    contacts = ContactsClient(settings)
    try:
        yield AppContext(settings=settings, mail=mail, calendar=calendar, contacts=contacts)
    finally:
        mail.close()
        calendar.close()
        contacts.close()


mcp = FastMCP("icloud", lifespan=lifespan)


def _ctx(ctx: Context) -> AppContext:
    return ctx.request_context.lifespan_context


class Confirm(BaseModel):
    """Minimal elicitation schema — the accept/decline action is what matters."""

    confirm: bool = True


def _require_writes(app: AppContext) -> None:
    if not app.settings.enable_writes:
        raise ValueError(
            "Writes are disabled. Set ICLOUD_ENABLE_WRITES=1 in the server "
            "environment to enable mail/calendar mutations."
        )


async def _confirmed(ctx: Context, app: AppContext, message: str) -> bool:
    """Gate a mutation: require writes enabled (raises if not) + user confirmation."""
    _require_writes(app)
    result = await ctx.elicit(message=message, schema=Confirm)
    return result.action == "accept"


def _window(app: AppContext, days_ahead: int) -> tuple[datetime, datetime]:
    today = datetime.now(app.settings.tz).date()
    start = datetime.combine(today, time.min, tzinfo=app.settings.tz)
    end = datetime.combine(today + timedelta(days=days_ahead), time.max, tzinfo=app.settings.tz)
    return start, end


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------

@mcp.tool()
async def list_mail(
    ctx: Context, folder: str = "INBOX", since_date: str = "", limit: int = 50
) -> list[Message]:
    """List messages in a folder (default INBOX), newest first.

    `since_date` is an optional YYYY-MM-DD; when set, only messages on/after that
    date are returned (server-side IMAP SINCE, which is date-granular and evaluated
    in the server's timezone).
    """
    app = _ctx(ctx)
    since = date.fromisoformat(since_date) if since_date else None
    return await _run_blocking(mail_ops.list_messages, app.mail, folder, since, limit)


@mcp.tool()
async def search_mail(
    ctx: Context, query: str, folder: str = "INBOX", limit: int = 50
) -> list[Message]:
    """Full-text search mail in a folder (default INBOX). Returns newest first."""
    app = _ctx(ctx)
    return await _run_blocking(mail_ops.search_messages, app.mail, query, folder, limit)


@mcp.tool()
async def list_folders(ctx: Context) -> list[Folder]:
    """List the IMAP mailbox folder names (e.g. INBOX, Sent Messages, Archive)."""
    app = _ctx(ctx)
    return await _run_blocking(mail_ops.list_folders, app.mail)


@mcp.tool()
async def get_message(ctx: Context, uid: int, folder: str = "INBOX") -> FullMessage | None:
    """Fetch a single message by UID, including its full text body."""
    app = _ctx(ctx)
    return await _run_blocking(mail_ops.get_message, app.mail, uid, folder)


@mcp.tool()
async def list_events(ctx: Context, days_ahead: int = 1) -> list[Event]:
    """List calendar events from today through `days_ahead` days out."""
    app = _ctx(ctx)
    start, end = _window(app, days_ahead)
    return await _run_blocking(cal_ops.list_events, app.calendar, start, end, app.settings.tz)


@mcp.tool()
async def search_events(ctx: Context, text: str, days_ahead: int = 30) -> list[Event]:
    """Find events whose title matches `text` within the next `days_ahead` days."""
    app = _ctx(ctx)
    start, end = _window(app, days_ahead)
    return await _run_blocking(
        cal_ops.search_events, app.calendar, text, start, end, app.settings.tz
    )


@mcp.tool()
async def list_calendars(ctx: Context) -> list[Calendar]:
    """List the names of all available calendars."""
    app = _ctx(ctx)
    return await _run_blocking(cal_ops.list_calendars, app.calendar)


@mcp.tool()
async def list_contacts(ctx: Context, limit: int = 200) -> list[Contact]:
    """List contacts from the iCloud address book (name, emails, phones, org)."""
    app = _ctx(ctx)
    return await _run_blocking(contacts_ops.list_contacts, app.contacts, limit)


@mcp.tool()
async def search_contacts(ctx: Context, query: str, limit: int = 50) -> list[Contact]:
    """Find contacts matching `query` across name, org, and email addresses."""
    app = _ctx(ctx)
    return await _run_blocking(contacts_ops.search_contacts, app.contacts, query, limit)


# --------------------------------------------------------------------------
# Mutating tools (opt-in via ICLOUD_ENABLE_WRITES, each requires confirmation)
# --------------------------------------------------------------------------

@mcp.tool()
async def send_mail(
    ctx: Context, to: str, subject: str, body: str, cc: str = ""
) -> str:
    """Send a plain-text email. Requires writes enabled + interactive confirmation."""
    app = _ctx(ctx)
    summary = f"Send email to {to}" + (f" (cc {cc})" if cc else "") + f" — “{subject}”?"
    if not await _confirmed(ctx, app, summary):
        return "Cancelled — email not sent."
    return await _run_write(write_ops.send_message, app.settings, to, subject, body, cc)


@mcp.tool()
async def create_event(
    ctx: Context,
    title: str,
    start: str,
    end: str,
    calendar: str = "",
    location: str = "",
) -> str:
    """Create a calendar event. `start`/`end` are ISO-8601 (e.g. 2026-06-21T14:00:00).

    Requires writes enabled + interactive confirmation.
    """
    app = _ctx(ctx)
    where = f" at {location}" if location else ""
    if not await _confirmed(ctx, app, f"Create event “{title}” from {start} to {end}{where}?"):
        return "Cancelled — event not created."
    return await _run_write(
        write_ops.create_event, app.calendar, title, start, end, calendar, location
    )


@mcp.tool()
async def update_event(
    ctx: Context,
    uid: str,
    title: str = "",
    start: str = "",
    end: str = "",
    location: str = "",
) -> str:
    """Edit an existing event (by UID). Empty fields are left unchanged.

    Requires writes enabled + interactive confirmation.
    """
    app = _ctx(ctx)
    changes = ", ".join(
        f"{k}={v}" for k, v in (("title", title), ("start", start), ("end", end), ("location", location)) if v
    ) or "(no fields given)"
    if not await _confirmed(ctx, app, f"Update event {uid}: {changes}?"):
        return "Cancelled — event not updated."
    return await _run_write(
        write_ops.update_event, app.calendar, uid, title, start, end, location
    )


@mcp.tool()
async def delete_event(ctx: Context, uid: str) -> str:
    """Delete a calendar event by UID. Requires writes enabled + interactive confirmation."""
    app = _ctx(ctx)
    if not await _confirmed(ctx, app, f"Permanently delete event {uid}?"):
        return "Cancelled — event not deleted."
    return await _run_write(write_ops.delete_event, app.calendar, uid)


@mcp.tool()
async def reply_mail(
    ctx: Context, uid: int, body: str, folder: str = "INBOX", reply_all: bool = False
) -> str:
    """Reply to message `uid` (quoting the original). Writes enabled + confirmation."""
    app = _ctx(ctx)
    _require_writes(app)
    reply_ctx = await _run_blocking(mail_ops.reply_context, app.mail, uid, folder)
    if reply_ctx is None:
        return f"Message {uid} not found in {folder}."
    scope = "all recipients" if reply_all else reply_ctx["from_addr"]
    result = await ctx.elicit(
        message=f"Reply to {scope} re “{reply_ctx['subject']}”?", schema=Confirm
    )
    if result.action != "accept":
        return "Cancelled — reply not sent."
    return await _run_write(
        write_ops.reply_message, app.settings, reply_ctx, body, reply_all
    )


@mcp.tool()
async def forward_mail(
    ctx: Context, uid: int, to: str, body: str = "", folder: str = "INBOX"
) -> str:
    """Forward message `uid` to `to`. Writes enabled + confirmation."""
    app = _ctx(ctx)
    _require_writes(app)
    fwd_ctx = await _run_blocking(mail_ops.reply_context, app.mail, uid, folder)
    if fwd_ctx is None:
        return f"Message {uid} not found in {folder}."
    result = await ctx.elicit(
        message=f"Forward “{fwd_ctx['subject']}” to {to}?", schema=Confirm
    )
    if result.action != "accept":
        return "Cancelled — message not forwarded."
    return await _run_write(write_ops.forward_message, app.settings, fwd_ctx, to, body)


@mcp.tool()
async def create_contact(
    ctx: Context,
    full_name: str,
    emails: list[str] | None = None,
    phones: list[str] | None = None,
    org: str = "",
) -> str:
    """Create a new contact. Requires writes enabled + interactive confirmation."""
    app = _ctx(ctx)
    if not await _confirmed(ctx, app, f"Create contact “{full_name}”?"):
        return "Cancelled — contact not created."
    return await _run_write(
        write_ops.create_contact, app.contacts, full_name, emails, phones, org
    )


@mcp.tool()
async def update_contact(
    ctx: Context,
    uid: str,
    full_name: str = "",
    emails: list[str] | None = None,
    phones: list[str] | None = None,
    org: str = "",
) -> str:
    """Edit a contact by UID (from list/search_contacts). Empty/None fields unchanged.

    Requires writes enabled + interactive confirmation.
    """
    app = _ctx(ctx)
    if not await _confirmed(ctx, app, f"Update contact {uid}?"):
        return "Cancelled — contact not updated."
    return await _run_write(
        write_ops.update_contact, app.contacts, uid, full_name, emails, phones, org
    )


@mcp.tool()
async def delete_contact(ctx: Context, uid: str) -> str:
    """Delete a contact by UID. Requires writes enabled + interactive confirmation."""
    app = _ctx(ctx)
    if not await _confirmed(ctx, app, f"Permanently delete contact {uid}?"):
        return "Cancelled — contact not deleted."
    return await _run_write(write_ops.delete_contact, app.contacts, uid)


# --------------------------------------------------------------------------
# Resources (passive context)
# --------------------------------------------------------------------------

@mcp.resource("icloud://mail/inbox/recent")
async def recent_inbox() -> list[Message]:
    """The most recent inbox messages."""
    app: AppContext = mcp.get_context().request_context.lifespan_context
    return await _run_blocking(mail_ops.list_messages, app.mail, "INBOX", None, 25)


@mcp.resource("icloud://calendar/today")
async def calendar_today() -> list[Event]:
    """Today + tomorrow's calendar events."""
    app: AppContext = mcp.get_context().request_context.lifespan_context
    start, end = _window(app, 1)
    return await _run_blocking(cal_ops.list_events, app.calendar, start, end, app.settings.tz)


# --------------------------------------------------------------------------
# Phase 2 (not implemented): mutating tools.
#
# send_mail / create_event / update_event / delete_event would go here, each
# gated behind ctx.elicit(...) for explicit user confirmation and backed by a
# SEPARATE SMTP/write credential + connection, so the read path above can never
# acquire write capability. Deliberately omitted from v1.
# --------------------------------------------------------------------------


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
