"""Smoke-check the real third-party APIs this package calls.

The other tests replace these libraries with fakes, so a breaking release
(a renamed method, a dropped parameter) would pass them and only fail at
runtime. These checks run against the installed libraries, without network.
"""

import inspect

import caldav
import icalendar
import imapclient
import vobject
from caldav.davclient import DAVClient, DAVResponse


def _params(fn):
    return inspect.signature(fn).parameters


def test_imapclient_api():
    params = _params(imapclient.IMAPClient.__init__)
    for name in ("ssl", "port", "use_uid", "timeout"):
        assert name in params
    for method in ("login", "logout", "select_folder", "search", "fetch", "list_folders"):
        assert hasattr(imapclient.IMAPClient, method)


def test_caldav_api():
    params = _params(DAVClient.__init__)
    for name in ("url", "username", "password", "timeout"):
        assert name in params
    assert hasattr(DAVClient, "request") and hasattr(DAVClient, "principal")
    assert hasattr(DAVResponse, "raw")
    for method in ("events", "save_event", "get_display_name"):
        assert hasattr(caldav.Calendar, method)
    for method in ("save", "delete", "data"):
        assert hasattr(caldav.Event, method)


def test_icalendar_round_trip():
    raw = b"BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:x\r\nSUMMARY:Hi\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    cal = icalendar.Calendar.from_ical(raw)
    vevent = next(c for c in cal.walk() if c.name == "VEVENT")
    vevent.add("location", "Here")
    assert b"LOCATION:Here" in cal.to_ical()


def test_vobject_round_trip():
    card = vobject.readOne("BEGIN:VCARD\r\nVERSION:3.0\r\nFN:A B\r\nEND:VCARD\r\n")
    card.add("email").value = "a@b.c"
    assert "a@b.c" in card.serialize()


def test_mcp_fastmcp_api():
    from mcp.server.fastmcp import Context, FastMCP  # noqa: F401
    from mcp.server.fastmcp.exceptions import ToolError  # noqa: F401

    assert hasattr(Context, "elicit")
