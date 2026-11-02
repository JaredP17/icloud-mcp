"""Unit tests for the mutating ops — SMTP send + CalDAV event creation. No network."""

import pytest

from icloud_mcp import writes


class _FakeSMTP:
    """Captures what would be sent, mirroring smtplib.SMTP's context-manager API."""

    last = None

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port
        self.started_tls = False
        self.tls_context = None
        self.logged_in = None
        self.sent = None
        _FakeSMTP.last = self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        self.started_tls = True
        self.tls_context = context

    def login(self, user, password):
        self.logged_in = (user, password)

    def send_message(self, msg):
        self.sent = msg


def _settings():
    return type(
        "S",
        (),
        {
            "username": "me@icloud.com",
            "smtp_host": "smtp.test",
            "smtp_port": 587,
            "keyring_service": "svc",
        },
    )()


def test_send_message_builds_and_sends(monkeypatch):
    monkeypatch.setattr(writes.smtplib, "SMTP", _FakeSMTP)
    monkeypatch.setattr(writes, "get_password", lambda svc, user: "app-pw")

    result = writes.send_message(
        _settings(), to="you@example.com", subject="Hi", body="Hello there.", cc="c@example.com"
    )
    sent = _FakeSMTP.last
    assert sent.started_tls and sent.logged_in == ("me@icloud.com", "app-pw")
    assert sent.sent["To"] == "you@example.com"
    assert sent.sent["Cc"] == "c@example.com"
    assert sent.sent["From"] == "me@icloud.com"
    assert sent.sent["Subject"] == "Hi"
    assert sent.sent.get_content().strip() == "Hello there."
    assert result == "Sent to you@example.com."


def test_send_message_verifies_tls_certificate(monkeypatch):
    import ssl

    monkeypatch.setattr(writes.smtplib, "SMTP", _FakeSMTP)
    monkeypatch.setattr(writes, "get_password", lambda svc, user: "app-pw")

    writes.send_message(_settings(), "you@example.com", "Hi", "Body")
    ctx = _FakeSMTP.last.tls_context
    assert ctx is not None
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname


def test_send_message_auth_error_is_wrapped(monkeypatch):
    import smtplib

    class _AuthFailSMTP(_FakeSMTP):
        def login(self, user, password):
            raise smtplib.SMTPAuthenticationError(535, b"nope")

    monkeypatch.setattr(writes.smtplib, "SMTP", _AuthFailSMTP)
    monkeypatch.setattr(writes, "get_password", lambda svc, user: "app-pw")

    with pytest.raises(writes.WriteError, match="authentication failed"):
        writes.send_message(_settings(), "you@example.com", "Hi", "Body")


class _FakeCal:
    def __init__(self, name):
        self._name = name
        self.saved = None

    def get_display_name(self):
        return self._name

    def save_event(self, ical):
        self.saved = ical


class _FakePrincipal:
    def __init__(self, cals):
        self._cals = cals

    def calendars(self):
        return self._cals


class _FakeCalClient:
    def __init__(self, cals):
        self._principal = _FakePrincipal(cals)

    def principal(self):
        return self._principal


def test_create_event_builds_vevent_on_default_calendar():
    home, work = _FakeCal("Home"), _FakeCal("Work")
    client = _FakeCalClient([home, work])
    msg = writes.create_event(
        client, "Dentist", "2026-06-21T14:00:00", "2026-06-21T15:00:00"
    )
    assert home.saved is not None and work.saved is None  # defaults to first
    assert "SUMMARY:Dentist" in home.saved
    assert "DTSTART:20260621T140000" in home.saved
    assert "DTEND:20260621T150000" in home.saved
    assert "Home" in msg


def test_create_event_targets_named_calendar_with_location():
    home, work = _FakeCal("Home"), _FakeCal("Work")
    client = _FakeCalClient([home, work])
    writes.create_event(
        client, "Standup", "2026-06-22T09:00:00", "2026-06-22T09:15:00",
        calendar_name="Work", location="Room 4",
    )
    assert work.saved is not None and home.saved is None
    assert "LOCATION:Room 4" in work.saved


def test_create_event_bad_date_is_wrapped():
    client = _FakeCalClient([_FakeCal("Home")])
    with pytest.raises(writes.WriteError, match="Invalid date"):
        writes.create_event(client, "X", "not-a-date", "also-bad")


def test_create_event_no_calendars():
    client = _FakeCalClient([])
    with pytest.raises(writes.WriteError, match="No calendars"):
        writes.create_event(client, "X", "2026-06-21T14:00:00", "2026-06-21T15:00:00")


# --- update_event / delete_event -----------------------------------------

_SEED_ICAL = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\n"
    "BEGIN:VEVENT\r\nUID:evt-1\r\nDTSTAMP:20260101T000000Z\r\n"
    "DTSTART:20260621T140000\r\nDTEND:20260621T150000\r\nSUMMARY:Old Title\r\n"
    "END:VEVENT\r\nEND:VCALENDAR\r\n"
)


class _FakeEvent:
    def __init__(self, data):
        self.data = data
        self.saved = False
        self.deleted = False

    def save(self):
        self.saved = True

    def delete(self):
        self.deleted = True


class _FakeCalWithEvents:
    def __init__(self, events):
        self._events = events  # uid -> _FakeEvent

    def events(self):
        # _find_event matches a "UID:<uid>" line in each event's .data.
        return list(self._events.values())


class _FakeCalClientEvents:
    def __init__(self, events):
        self._cal = _FakeCalWithEvents(events)

    def principal(self):
        return type("P", (), {"calendars": lambda self_: [self._cal]})()


def test_update_event_edits_only_given_fields():
    ev = _FakeEvent(_SEED_ICAL)
    client = _FakeCalClientEvents({"evt-1": ev})
    writes.update_event(client, "evt-1", title="New Title", location="Room 9")
    assert ev.saved
    assert "SUMMARY:New Title" in ev.data
    assert "LOCATION:Room 9" in ev.data
    assert "DTSTART:20260621T140000" in ev.data  # untouched


def test_update_event_not_found():
    client = _FakeCalClientEvents({})
    with pytest.raises(writes.WriteError, match="not found"):
        writes.update_event(client, "missing", title="x")


def test_delete_event_calls_delete():
    ev = _FakeEvent(_SEED_ICAL)
    client = _FakeCalClientEvents({"evt-1": ev})
    writes.delete_event(client, "evt-1")
    assert ev.deleted


# --- reply / forward ------------------------------------------------------

_CTX = {
    "message_id": "<abc@mail>",
    "references": "<root@mail>",
    "from_addr": "alice@example.com",
    "to": "me@icloud.com, bob@example.com",
    "cc": "carol@example.com",
    "subject": "Project plan",
    "date": "Sat, 20 Jun 2026 10:00:00 +0000",
    "body": "Original line one.\nOriginal line two.",
}


def _capture_send(monkeypatch):
    sent = {}
    monkeypatch.setattr(writes, "_send", lambda settings, msg: sent.update(msg=msg))
    return sent


def test_reply_message_sets_threading_and_quotes(monkeypatch):
    sent = _capture_send(monkeypatch)
    writes.reply_message(_settings(), _CTX, "My reply.")
    msg = sent["msg"]
    assert msg["To"] == "alice@example.com"
    assert msg["Subject"] == "Re: Project plan"
    assert msg["In-Reply-To"] == "<abc@mail>"
    assert msg["References"] == "<root@mail> <abc@mail>"
    content = msg.get_content()
    assert content.startswith("My reply.")
    assert "> Original line one." in content
    assert "alice@example.com wrote:" in content


def test_reply_all_adds_other_recipients_minus_self(monkeypatch):
    sent = _capture_send(monkeypatch)
    writes.reply_message(_settings(), _CTX, "Reply.", reply_all=True)
    cc = sent["msg"]["Cc"]
    assert "bob@example.com" in cc and "carol@example.com" in cc
    assert "me@icloud.com" not in cc  # self excluded
    assert "alice@example.com" not in (cc or "")  # primary recipient not duplicated


def test_reply_message_keeps_existing_re_prefix(monkeypatch):
    sent = _capture_send(monkeypatch)
    ctx = {**_CTX, "subject": "Re: Project plan"}
    writes.reply_message(_settings(), ctx, "ok")
    assert sent["msg"]["Subject"] == "Re: Project plan"


def test_forward_message_builds_forward_block(monkeypatch):
    sent = _capture_send(monkeypatch)
    writes.forward_message(_settings(), _CTX, "fwd@example.com", "FYI.")
    msg = sent["msg"]
    assert msg["To"] == "fwd@example.com"
    assert msg["Subject"] == "Fwd: Project plan"
    content = msg.get_content()
    assert content.startswith("FYI.")
    assert "Forwarded message" in content
    assert "From: alice@example.com" in content
    assert "Original line one." in content


# --- contact create / update / delete -------------------------------------

class _FakeContactsClient:
    def __init__(self, entries=None):
        self._entries = entries or {}  # uid -> (href, text)
        self.put = None
        self.deleted = None

    def addressbook_url(self):
        return "https://card.test/home/book/"

    def find_entry_by_uid(self, uid):
        return self._entries.get(uid)

    def put_vcard(self, url, text):
        self.put = (url, text)

    def delete_resource(self, url):
        self.deleted = url


def test_create_contact_puts_vcard():
    client = _FakeContactsClient()
    msg = writes.create_contact(client, "Grace Hopper", ["grace@navy.mil"], ["+1-555-7"])
    url, text = client.put
    assert url.startswith("https://card.test/home/book/") and url.endswith(".vcf")
    assert "FN:Grace Hopper" in text
    assert "grace@navy.mil" in text
    assert "uid" in msg.lower()


def test_update_contact_merges_and_preserves_other_fields():
    existing = (
        "BEGIN:VCARD\r\nVERSION:3.0\r\nUID:c-1\r\nFN:Old Name\r\n"
        "EMAIL:old@x.com\r\nNOTE:keep me\r\nEND:VCARD\r\n"
    )
    client = _FakeContactsClient({"c-1": ("https://card.test/home/book/c-1.vcf", existing)})
    writes.update_contact(client, "c-1", full_name="New Name", emails=["new@x.com"])
    url, text = client.put
    assert url.endswith("c-1.vcf")
    assert "FN:New Name" in text
    assert "new@x.com" in text and "old@x.com" not in text  # emails replaced
    assert "NOTE:keep me" in text  # unmodeled field preserved


def test_update_contact_not_found():
    client = _FakeContactsClient()
    with pytest.raises(writes.WriteError, match="not found"):
        writes.update_contact(client, "nope", full_name="x")


def test_delete_contact_deletes_href():
    client = _FakeContactsClient({"c-1": ("https://card.test/home/book/c-1.vcf", "x")})
    writes.delete_contact(client, "c-1")
    assert client.deleted == "https://card.test/home/book/c-1.vcf"
