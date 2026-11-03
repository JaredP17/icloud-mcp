"""End-to-end MCP tool tests via the in-memory client/server session.

The lifespan's real iCloud clients are replaced with in-process fakes, so these
tests exercise the full tool dispatch path without any network or credentials.
"""

import email as email_lib

import pytest

import icloud_mcp.server as server


@pytest.fixture
def anyio_backend():
    return "asyncio"


class _FakeIMAP:
    def __init__(self, bodies):
        self._bodies = bodies

    def select_folder(self, folder, readonly=True):
        pass

    def search(self, criteria):
        return list(self._bodies)

    def fetch(self, uids, parts):
        # Mirror imapclient: absent UIDs simply aren't in the result dict.
        return {uid: {b"BODY[]": self._bodies[uid]} for uid in uids if uid in self._bodies}


class _FakeMailClient:
    def __init__(self, bodies):
        self._imap = _FakeIMAP(bodies)

    def select(self, folder="INBOX"):
        return self._imap

    def close(self):
        pass


class _FakeCalendarClient:
    def principal(self):
        class _P:
            def calendars(self):
                return []

        return _P()

    def close(self):
        pass


class _FakeContactsClient:
    def fetch_vcards(self):
        return []

    def close(self):
        pass


def _install_fakes(monkeypatch, enable_writes=False):
    body = (
        b"From: bank@example.com\r\nSubject: Statement ready\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n\r\nYour statement is ready.\r\n"
    )
    fake_settings = type(
        "S", (), {"username": "x", "keyring_service": "x", "enable_writes": enable_writes}
    )()
    monkeypatch.setattr(server.Settings, "from_env", classmethod(lambda cls: fake_settings))
    monkeypatch.setattr(server, "MailClient", lambda s: _FakeMailClient({42: body}))
    monkeypatch.setattr(server, "CalendarClient", lambda s: _FakeCalendarClient())
    monkeypatch.setattr(server, "ContactsClient", lambda s: _FakeContactsClient())
    return fake_settings


@pytest.fixture
def patched_clients(monkeypatch):
    _install_fakes(monkeypatch, enable_writes=False)


@pytest.fixture
def patched_clients_writable(monkeypatch):
    """Writes enabled, with the write ops stubbed so nothing leaves the process."""
    _install_fakes(monkeypatch, enable_writes=True)
    calls = {}

    def fake_send(settings, to, subject, body, cc=""):
        calls["sent"] = (to, subject)
        return f"Sent to {to}."

    def fake_create(client, title, start, end, calendar="", location=""):
        calls["event"] = (title, start, end)
        return f"Created “{title}”."

    def record(name):
        def fn(*a, **k):
            calls[name] = (a, k)
            return f"{name} ok"
        return fn

    monkeypatch.setattr(server.write_ops, "send_message", fake_send)
    monkeypatch.setattr(server.write_ops, "create_event", fake_create)
    for op in (
        "update_event", "delete_event", "reply_message", "forward_message",
        "create_contact", "update_contact", "delete_contact",
    ):
        monkeypatch.setattr(server.write_ops, op, record(op))
    return calls


def _accept_cb(action):
    """Build an elicitation callback that always returns `action`."""
    from mcp.types import ElicitResult

    async def cb(context, params):
        return ElicitResult(action=action, content={"confirm": action == "accept"})

    return cb


@pytest.mark.anyio
async def test_search_mail_tool(patched_clients):
    from mcp.shared.memory import create_connected_server_and_client_session

    async with create_connected_server_and_client_session(server.mcp._mcp_server) as client:
        result = await client.call_tool("search_mail", {"query": "statement"})
        assert not result.isError
        assert any("Statement ready" in str(c.text) for c in result.content)


@pytest.mark.anyio
async def test_list_mail_tool(patched_clients):
    from mcp.shared.memory import create_connected_server_and_client_session

    async with create_connected_server_and_client_session(server.mcp._mcp_server) as client:
        result = await client.call_tool("list_mail", {"since_date": "2026-06-20"})
        assert not result.isError
        assert any("Statement ready" in str(c.text) for c in result.content)


@pytest.mark.anyio
async def test_list_calendars_tool_empty(patched_clients):
    from mcp.shared.memory import create_connected_server_and_client_session

    async with create_connected_server_and_client_session(server.mcp._mcp_server) as client:
        result = await client.call_tool("list_calendars", {})
        assert not result.isError


@pytest.mark.anyio
async def test_send_mail_blocked_when_writes_disabled(patched_clients):
    from mcp.shared.memory import create_connected_server_and_client_session

    async with create_connected_server_and_client_session(server.mcp._mcp_server) as client:
        result = await client.call_tool(
            "send_mail", {"to": "x@y.com", "subject": "Hi", "body": "yo"}
        )
        assert result.isError
        assert any("ICLOUD_ENABLE_WRITES" in str(c.text) for c in result.content)


@pytest.mark.anyio
async def test_send_mail_declined_does_not_send(patched_clients_writable):
    from mcp.shared.memory import create_connected_server_and_client_session

    async with create_connected_server_and_client_session(
        server.mcp._mcp_server, elicitation_callback=_accept_cb("decline")
    ) as client:
        result = await client.call_tool(
            "send_mail", {"to": "x@y.com", "subject": "Hi", "body": "yo"}
        )
        assert not result.isError
        assert any("Cancelled" in str(c.text) for c in result.content)
        assert "sent" not in patched_clients_writable


@pytest.mark.anyio
async def test_send_mail_accepted_sends(patched_clients_writable):
    from mcp.shared.memory import create_connected_server_and_client_session

    async with create_connected_server_and_client_session(
        server.mcp._mcp_server, elicitation_callback=_accept_cb("accept")
    ) as client:
        result = await client.call_tool(
            "send_mail", {"to": "x@y.com", "subject": "Hi", "body": "yo"}
        )
        assert not result.isError
        assert patched_clients_writable["sent"] == ("x@y.com", "Hi")


# Every write tool, the write op it should reach, and valid arguments for it.
_WRITE_TOOLS = [
    ("send_mail", "send_message", {"to": "x@y.com", "subject": "Hi", "body": "yo"}),
    ("create_event", "create_event", {"title": "T", "start": "2026-06-21T14:00:00", "end": "2026-06-21T15:00:00"}),
    ("update_event", "update_event", {"uid": "e1", "title": "T2"}),
    ("delete_event", "delete_event", {"uid": "e1"}),
    ("reply_mail", "reply_message", {"uid": 42, "body": "thanks"}),
    ("forward_mail", "forward_message", {"uid": 42, "to": "z@y.com"}),
    ("create_contact", "create_contact", {"full_name": "Ann"}),
    ("update_contact", "update_contact", {"uid": "c1", "org": "Acme"}),
    ("delete_contact", "delete_contact", {"uid": "c1"}),
]


@pytest.fixture
def own_executor(monkeypatch):
    """A private worker pool, drained at teardown, so slow ops can't leak across tests."""
    from concurrent.futures import ThreadPoolExecutor

    pool = ThreadPoolExecutor(max_workers=4)
    monkeypatch.setattr(server, "_executor", pool)
    yield pool
    pool.shutdown(wait=True, cancel_futures=True)


def _text(result):
    return " ".join(str(c.text) for c in result.content)


@pytest.mark.anyio
@pytest.mark.parametrize("tool,op,args", _WRITE_TOOLS)
async def test_write_timeout_warns_not_to_retry(
    tool, op, args, patched_clients_writable, own_executor, monkeypatch
):
    import threading

    from mcp.shared.memory import create_connected_server_and_client_session

    release = threading.Event()
    monkeypatch.setattr(server, "_WRITE_TIMEOUT", 0.05)
    monkeypatch.setattr(server.write_ops, op, lambda *a, **k: release.wait(5) and "done")

    try:
        async with create_connected_server_and_client_session(
            server.mcp._mcp_server, elicitation_callback=_accept_cb("accept")
        ) as client:
            result = await client.call_tool(tool, args)
            assert result.isError
            assert "may still complete" in _text(result) and "Do not retry" in _text(result)
    finally:
        release.set()


@pytest.mark.anyio
async def test_write_timeout_while_queued_is_safe_to_retry(
    patched_clients_writable, own_executor, monkeypatch
):
    import threading

    from mcp.shared.memory import create_connected_server_and_client_session

    release = threading.Event()
    for _ in range(4):  # fill every worker so the write can't start
        own_executor.submit(release.wait, 5)
    monkeypatch.setattr(server, "_WRITE_TIMEOUT", 0.05)

    try:
        async with create_connected_server_and_client_session(
            server.mcp._mcp_server, elicitation_callback=_accept_cb("accept")
        ) as client:
            result = await client.call_tool(
                "send_mail", {"to": "x@y.com", "subject": "Hi", "body": "yo"}
            )
            assert result.isError
            assert "nothing was changed" in _text(result)
    finally:
        release.set()
    own_executor.shutdown(wait=True)
    assert "sent" not in patched_clients_writable  # the queued send never ran


@pytest.mark.anyio
async def test_write_error_is_reported_not_masked(patched_clients_writable, monkeypatch):
    from mcp.shared.memory import create_connected_server_and_client_session

    def failing(*a, **k):
        raise server.write_ops.WriteError("SMTP authentication failed")

    monkeypatch.setattr(server.write_ops, "send_message", failing)

    async with create_connected_server_and_client_session(
        server.mcp._mcp_server, elicitation_callback=_accept_cb("accept")
    ) as client:
        result = await client.call_tool(
            "send_mail", {"to": "x@y.com", "subject": "Hi", "body": "yo"}
        )
        assert result.isError
        assert "SMTP authentication failed" in _text(result)
        assert "may still complete" not in _text(result)


@pytest.mark.anyio
async def test_late_write_outcome_is_logged(
    patched_clients_writable, own_executor, monkeypatch, caplog
):
    import logging
    import threading

    from mcp.shared.memory import create_connected_server_and_client_session

    release = threading.Event()

    def slow_fail(*a, **k):
        release.wait(5)
        raise server.write_ops.WriteError("SMTP authentication failed")

    monkeypatch.setattr(server, "_WRITE_TIMEOUT", 0.05)
    monkeypatch.setattr(server.write_ops, "send_message", slow_fail)

    with caplog.at_level(logging.WARNING, logger=server.log.name):
        async with create_connected_server_and_client_session(
            server.mcp._mcp_server, elicitation_callback=_accept_cb("accept")
        ) as client:
            result = await client.call_tool(
                "send_mail", {"to": "x@y.com", "subject": "Hi", "body": "yo"}
            )
            assert result.isError
        release.set()
        own_executor.shutdown(wait=True)  # let the late failure land

    assert any(
        "slow_fail" in r.getMessage() and "FAILED" in r.getMessage() for r in caplog.records
    )


@pytest.mark.anyio
async def test_calls_on_one_client_do_not_overlap(own_executor):
    import threading
    import time

    from anyio import create_task_group

    class _Client:
        lock = threading.RLock()

    active, peak = 0, 0
    guard = threading.Lock()

    def op(client):
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
        time.sleep(0.05)
        with guard:
            active -= 1

    client = _Client()
    async with create_task_group() as tg:
        for _ in range(4):
            tg.start_soon(server._run_blocking, op, client)
    assert peak == 1


@pytest.mark.anyio
async def test_create_event_accepted_creates(patched_clients_writable):
    from mcp.shared.memory import create_connected_server_and_client_session

    async with create_connected_server_and_client_session(
        server.mcp._mcp_server, elicitation_callback=_accept_cb("accept")
    ) as client:
        result = await client.call_tool(
            "create_event",
            {"title": "Sync", "start": "2026-06-21T14:00:00", "end": "2026-06-21T15:00:00"},
        )
        assert not result.isError
        assert "event" in patched_clients_writable


@pytest.mark.anyio
async def test_update_event_blocked_when_writes_disabled(patched_clients):
    from mcp.shared.memory import create_connected_server_and_client_session

    async with create_connected_server_and_client_session(server.mcp._mcp_server) as client:
        result = await client.call_tool("update_event", {"uid": "e1", "title": "x"})
        assert result.isError
        assert any("ICLOUD_ENABLE_WRITES" in str(c.text) for c in result.content)


@pytest.mark.anyio
async def test_update_event_accepted(patched_clients_writable):
    from mcp.shared.memory import create_connected_server_and_client_session

    async with create_connected_server_and_client_session(
        server.mcp._mcp_server, elicitation_callback=_accept_cb("accept")
    ) as client:
        result = await client.call_tool("update_event", {"uid": "e1", "title": "New"})
        assert not result.isError
        assert "update_event" in patched_clients_writable


@pytest.mark.anyio
async def test_reply_mail_accepted(patched_clients_writable):
    from mcp.shared.memory import create_connected_server_and_client_session

    async with create_connected_server_and_client_session(
        server.mcp._mcp_server, elicitation_callback=_accept_cb("accept")
    ) as client:
        # uid 42 exists in the fake mailbox, so reply_context resolves.
        result = await client.call_tool("reply_mail", {"uid": 42, "body": "thanks"})
        assert not result.isError
        assert "reply_message" in patched_clients_writable


@pytest.mark.anyio
async def test_reply_mail_unknown_uid(patched_clients_writable):
    from mcp.shared.memory import create_connected_server_and_client_session

    async with create_connected_server_and_client_session(
        server.mcp._mcp_server, elicitation_callback=_accept_cb("accept")
    ) as client:
        result = await client.call_tool("reply_mail", {"uid": 999, "body": "x"})
        assert not result.isError
        assert any("not found" in str(c.text) for c in result.content)
        assert "reply_message" not in patched_clients_writable


@pytest.mark.anyio
async def test_create_contact_accepted(patched_clients_writable):
    from mcp.shared.memory import create_connected_server_and_client_session

    async with create_connected_server_and_client_session(
        server.mcp._mcp_server, elicitation_callback=_accept_cb("accept")
    ) as client:
        result = await client.call_tool("create_contact", {"full_name": "Ada Lovelace"})
        assert not result.isError
        assert "create_contact" in patched_clients_writable


@pytest.mark.anyio
async def test_delete_contact_declined(patched_clients_writable):
    from mcp.shared.memory import create_connected_server_and_client_session

    async with create_connected_server_and_client_session(
        server.mcp._mcp_server, elicitation_callback=_accept_cb("decline")
    ) as client:
        result = await client.call_tool("delete_contact", {"uid": "c1"})
        assert not result.isError
        assert any("Cancelled" in str(c.text) for c in result.content)
        assert "delete_contact" not in patched_clients_writable
