"""Mutating operations — the *only* module in the package that can change state.

Kept deliberately separate from the read path: mail is sent over a fresh SMTP
connection (the read-only IMAP ``MailClient`` has no send capability), and every
caller is expected to be gated by ``Settings.enable_writes`` + an interactive
confirmation in ``server.py`` before reaching here.
"""

from __future__ import annotations

import smtplib
import ssl
import uuid
from datetime import datetime, timezone
from email.message import EmailMessage

import icalendar
import vobject

from . import contacts as contacts_ops
from .auth import get_password
from .clients import CalendarClient, ContactsClient
from .config import Settings


# Verifies the server's certificate and hostname. Built once: loading the CA store
# isn't free, and an SSLContext is safe to share across threads.
_TLS_CONTEXT = ssl.create_default_context()


class WriteError(RuntimeError):
    """A user-facing failure from a mutating operation (no credential leakage)."""


def _send(settings: Settings, msg: EmailMessage) -> None:
    """Deliver a built message over a fresh, authenticated SMTP+STARTTLS connection."""
    password = get_password(settings.keyring_service, settings.username)
    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as smtp:
            # Plain starttls() uses an unverified context; ours checks the cert
            # before the password is sent.
            smtp.starttls(context=_TLS_CONTEXT)
            smtp.login(settings.username, password)
            smtp.send_message(msg)
    except smtplib.SMTPAuthenticationError as exc:
        raise WriteError(
            "SMTP authentication failed — check the app-specific password in the keyring."
        ) from exc
    except (smtplib.SMTPException, OSError) as exc:
        raise WriteError(f"Failed to send mail: {type(exc).__name__}") from exc


def send_message(settings: Settings, to: str, subject: str, body: str, cc: str = "") -> str:
    """Send a plain-text email."""
    msg = EmailMessage()
    msg["From"] = settings.username
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    msg["Subject"] = subject
    msg.set_content(body)
    _send(settings, msg)
    return f"Sent to {to}."


def _prefixed_subject(subject: str, prefix: str) -> str:
    """Add `Re:`/`Fwd:` unless the subject already carries it."""
    if subject.strip().lower().startswith(prefix.lower()):
        return subject
    return f"{prefix} {subject}".strip()


def _quote(ctx: dict) -> str:
    quoted = "\n".join(f"> {line}" for line in (ctx.get("body") or "").splitlines())
    attribution = f"On {ctx.get('date', '')}, {ctx.get('from_addr', '')} wrote:".strip()
    return f"{attribution}\n{quoted}"


def _split_addrs(value: str) -> list[str]:
    return [a.strip() for a in (value or "").split(",") if a.strip()]


def reply_message(settings: Settings, ctx: dict, body: str, reply_all: bool = False) -> str:
    """Build and send a reply to the message described by `ctx` (from mail.reply_context)."""
    msg = EmailMessage()
    msg["From"] = settings.username
    msg["To"] = ctx["from_addr"]
    if reply_all:
        me = settings.username.lower()
        extra = [
            a for a in _split_addrs(ctx.get("to", "")) + _split_addrs(ctx.get("cc", ""))
            if me not in a.lower() and ctx["from_addr"].lower() not in a.lower()
        ]
        if extra:
            msg["Cc"] = ", ".join(dict.fromkeys(extra))  # de-duped, order-preserving
    msg["Subject"] = _prefixed_subject(ctx.get("subject", ""), "Re:")
    if ctx.get("message_id"):
        msg["In-Reply-To"] = ctx["message_id"]
        msg["References"] = (ctx.get("references", "") + " " + ctx["message_id"]).strip()
    msg.set_content(f"{body}\n\n{_quote(ctx)}")
    _send(settings, msg)
    return f"Replied to {ctx['from_addr']}."


def forward_message(settings: Settings, ctx: dict, to: str, body: str = "") -> str:
    """Build and send a forward of the message described by `ctx` to `to`."""
    msg = EmailMessage()
    msg["From"] = settings.username
    msg["To"] = to
    msg["Subject"] = _prefixed_subject(ctx.get("subject", ""), "Fwd:")
    header = (
        "---------- Forwarded message ----------\n"
        f"From: {ctx.get('from_addr', '')}\n"
        f"Date: {ctx.get('date', '')}\n"
        f"Subject: {ctx.get('subject', '')}\n"
        f"To: {ctx.get('to', '')}\n"
    )
    intro = f"{body}\n\n" if body else ""
    msg.set_content(f"{intro}{header}\n{ctx.get('body', '')}")
    _send(settings, msg)
    return f"Forwarded to {to}."


def _ical_dt(value: str) -> str:
    """ISO-8601 string -> iCal date-time (raises ValueError on bad input)."""
    return datetime.fromisoformat(value).strftime("%Y%m%dT%H%M%S")


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _ical(title: str, start_iso: str, end_iso: str, location: str) -> str:
    """Build a minimal VEVENT. Start/end are ISO-8601 strings (date or datetime)."""
    fmt = _ical_dt
    uid = f"{uuid.uuid4()}@icloud-mcp"
    stamp = _stamp()
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//icloud-mcp//EN",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{stamp}",
        f"DTSTART:{fmt(start_iso)}",
        f"DTEND:{fmt(end_iso)}",
        f"SUMMARY:{title}",
    ]
    if location:
        lines.append(f"LOCATION:{location}")
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(lines)


def create_event(
    client: CalendarClient,
    title: str,
    start_iso: str,
    end_iso: str,
    calendar_name: str = "",
    location: str = "",
) -> str:
    """Create a calendar event. Targets `calendar_name` if given, else the first."""
    try:
        ical = _ical(title, start_iso, end_iso, location)
    except ValueError as exc:
        raise WriteError(f"Invalid date/time: {exc}") from exc

    principal = client.principal()
    calendars = principal.calendars()
    if not calendars:
        raise WriteError("No calendars available to write to.")

    target = calendars[0]
    if calendar_name:
        for cal in calendars:
            if cal.get_display_name() == calendar_name:
                target = cal
                break

    try:
        target.save_event(ical)
    except Exception as exc:  # caldav raises a variety of transport errors
        raise WriteError(f"Failed to create event: {type(exc).__name__}") from exc
    return f"Created “{title}” on {target.get_display_name()}."


def _find_event(client: CalendarClient, uid: str):
    """Locate a caldav event by iCal UID across all calendars, or raise WriteError.

    iCloud rejects the ``calendar-query`` REPORT that caldav's ``event_by_uid``
    issues (HTTP 412), so we list each calendar's events and match the UID line
    ourselves — reliable, at the cost of a listing per calendar.
    """
    needle = f"UID:{uid}"
    for cal in client.principal().calendars():
        try:
            events = cal.events()
        except Exception:
            # A calendar that can't be listed (e.g. a reminders list) is skipped.
            continue
        for event in events:
            if any(line.strip() == needle for line in event.data.splitlines()):
                return event
    raise WriteError(f"Event {uid} not found.")


def update_event(
    client: CalendarClient,
    uid: str,
    title: str = "",
    start_iso: str = "",
    end_iso: str = "",
    location: str = "",
) -> str:
    """Edit an existing event in place. Empty arguments are left unchanged."""
    event = _find_event(client, uid)
    cal = icalendar.Calendar.from_ical(event.data)
    vevent = next((c for c in cal.walk() if c.name == "VEVENT"), None)
    if vevent is None:
        raise WriteError(f"Event {uid} has no VEVENT to edit.")

    def _set(key: str, value: str):
        if key in vevent:
            del vevent[key]
        vevent.add(key, value)

    try:
        if title:
            _set("summary", title)
        if start_iso:
            _set("dtstart", datetime.fromisoformat(start_iso))
        if end_iso:
            _set("dtend", datetime.fromisoformat(end_iso))
        if location:
            _set("location", location)
    except ValueError as exc:
        raise WriteError(f"Invalid date/time: {exc}") from exc
    _set("dtstamp", datetime.now(timezone.utc))

    event.data = cal.to_ical().decode()
    try:
        event.save()
    except Exception as exc:
        raise WriteError(f"Failed to update event: {type(exc).__name__}") from exc
    return f"Updated event {uid}."


def delete_event(client: CalendarClient, uid: str) -> str:
    """Delete an event by iCal UID."""
    event = _find_event(client, uid)
    try:
        event.delete()
    except Exception as exc:
        raise WriteError(f"Failed to delete event: {type(exc).__name__}") from exc
    return f"Deleted event {uid}."


# --------------------------------------------------------------------------
# Contacts (CardDAV writes)
# --------------------------------------------------------------------------

def create_contact(
    client: ContactsClient,
    full_name: str,
    emails: list[str] | None = None,
    phones: list[str] | None = None,
    org: str = "",
) -> str:
    """Create a new contact in the (first) addressbook."""
    uid = str(uuid.uuid4()).upper()
    vcard = contacts_ops.build_vcard(uid, full_name, emails or [], phones or [], org)
    try:
        url = client.addressbook_url().rstrip("/") + f"/{uid}.vcf"
        client.put_vcard(url, vcard)
    except Exception as exc:
        raise WriteError(f"Failed to create contact: {type(exc).__name__}") from exc
    return f"Created contact “{full_name}” (uid {uid})."


def update_contact(
    client: ContactsClient,
    uid: str,
    full_name: str = "",
    emails: list[str] | None = None,
    phones: list[str] | None = None,
    org: str = "",
) -> str:
    """Edit a contact by UID, merging changes into the existing vCard.

    `full_name`/`org` empty strings and `emails`/`phones` None leave that field
    unchanged; passing an empty list for emails/phones clears that field.
    """
    entry = client.find_entry_by_uid(uid)
    if entry is None:
        raise WriteError(f"Contact {uid} not found.")
    href, text = entry
    card = vobject.readOne(text)

    if full_name:
        if hasattr(card, "fn"):
            card.fn.value = full_name
        else:
            card.add("fn").value = full_name
    if org:
        if hasattr(card, "org"):
            card.org.value = [org]
        else:
            card.add("org").value = [org]
    for field, values in (("email", emails), ("tel", phones)):
        if values is None:
            continue
        while field in card.contents:  # clear existing, then re-add
            del card.contents[field]
        for value in values:
            card.add(field).value = value

    try:
        client.put_vcard(href, card.serialize())
    except Exception as exc:
        raise WriteError(f"Failed to update contact: {type(exc).__name__}") from exc
    return f"Updated contact {uid}."


def delete_contact(client: ContactsClient, uid: str) -> str:
    """Delete a contact by UID."""
    entry = client.find_entry_by_uid(uid)
    if entry is None:
        raise WriteError(f"Contact {uid} not found.")
    try:
        client.delete_resource(entry[0])
    except Exception as exc:
        raise WriteError(f"Failed to delete contact: {type(exc).__name__}") from exc
    return f"Deleted contact {uid}."
