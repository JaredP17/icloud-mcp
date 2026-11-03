"""Connection-holding clients for IMAP and CalDAV.

The MCP server is a long-lived process, so each client lazily connects once and
reuses the connection across tool calls, rather than reconnecting every time.

IMAP is opened read-only and message bodies are fetched with ``BODY.PEEK[]``
(see ``mail.py``), so this client cannot mark messages read or mutate the
mailbox — a structural guarantee for the read-only v1.

Each client holds one connection that isn't thread-safe, so each carries a
``lock``. The server holds it for a whole operation (e.g. select folder + fetch),
so concurrent tool calls can't interleave commands on the same connection.
"""

from __future__ import annotations

import html
import imaplib
import re
import threading
from urllib.parse import urljoin

import caldav
import imapclient

from .auth import get_password
from .config import Settings

# Per-socket-operation timeout (seconds) for IMAP and CalDAV/CardDAV, so a stalled
# connection fails instead of holding a worker thread forever.
NET_TIMEOUT = 20

# Errors that indicate a dead/stale IMAP socket and warrant a single reconnect.
_IMAP_CONN_ERRORS = (OSError, imaplib.IMAP4.error, imapclient.exceptions.IMAPClientError)

_CARDDAV = "urn:ietf:params:xml:ns:carddav"
_HREF_RE = re.compile(r"<(?:\w+:)?href[^>]*>([^<]+)</(?:\w+:)?href>", re.I)
_RESPONSE_RE = re.compile(r"<(?:\w+:)?response[^>]*>(.*?)</(?:\w+:)?response>", re.I | re.S)
_ADDRDATA_RE = re.compile(r"<(?:\w+:)?address-data[^>]*>(.*?)</(?:\w+:)?address-data>", re.I | re.S)


def _href_within(element: str, raw: str) -> str | None:
    """The <href> nested inside a named element (not the response's own href)."""
    block = re.search(rf"<(?:\w+:)?{element}\b[^>]*>(.*?)</(?:\w+:)?{element}>", raw, re.I | re.S)
    if not block:
        return None
    href = _HREF_RE.search(block.group(1))
    return href.group(1).strip() if href else None


class MailClient:
    """Holds a logged-in, read-only IMAP connection."""

    def __init__(self, settings: Settings):
        self._settings = settings
        self._client: imapclient.IMAPClient | None = None
        self.lock = threading.RLock()

    def connection(self) -> imapclient.IMAPClient:
        if self._client is None:
            s = self._settings
            client = imapclient.IMAPClient(
                s.imap_host, ssl=True, port=s.imap_port, use_uid=True, timeout=NET_TIMEOUT
            )
            client.login(s.username, get_password(s.keyring_service, s.username))
            self._client = client
        return self._client

    def select(self, folder: str = "INBOX") -> imapclient.IMAPClient:
        # Reconnect once on a stale/dropped connection — long-lived servers commonly
        # see the socket time out while idle, which surfaces on the first command.
        for last_attempt in (False, True):
            client = self.connection()
            try:
                client.select_folder(folder, readonly=True)
                return client
            except _IMAP_CONN_ERRORS:
                self.close()
                if last_attempt:
                    raise

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.logout()
            except Exception:  # logging out a dead connection shouldn't mask the cause
                pass
            finally:
                self._client = None


class ContactsClient:
    """Read-only CardDAV access to iCloud contacts.

    ``caldav``'s ``DAVClient`` is CalDAV-only, but its generic WebDAV verbs
    (``propfind``/``report``/``request``) are enough to run the CardDAV discovery
    dance and pull vCards. Discovery is done once and the addressbook URLs cached.
    """

    def __init__(self, settings: Settings):
        self._settings = settings
        self._dav: caldav.DAVClient | None = None
        self._addressbooks: list[str] | None = None
        self.lock = threading.RLock()

    def _client(self) -> caldav.DAVClient:
        if self._dav is None:
            s = self._settings
            self._dav = caldav.DAVClient(
                url=s.carddav_url,
                username=s.username,
                password=get_password(s.keyring_service, s.username),
                timeout=NET_TIMEOUT,
            )
        return self._dav

    def _propfind(self, url: str, prop_xml: str, depth: str) -> str:
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<d:propfind xmlns:d="DAV:" xmlns:c="%s"><d:prop>%s</d:prop></d:propfind>'
            % (_CARDDAV, prop_xml)
        )
        return self._client().request(
            url, "PROPFIND", body, {"Depth": depth, "Content-Type": "application/xml"}
        ).raw

    def _addressbook_urls(self) -> list[str]:
        if self._addressbooks is not None:
            return self._addressbooks
        s = self._settings
        # 1. current-user-principal at the service root.
        raw = self._propfind(s.carddav_url, "<d:current-user-principal/>", "0")
        href = _href_within("current-user-principal", raw)
        if not href:
            self._addressbooks = []
            return self._addressbooks
        principal = urljoin(s.carddav_url, href)
        # 2. addressbook-home-set on the principal (returns a partitioned host).
        raw = self._propfind(principal, "<c:addressbook-home-set/>", "0")
        href = _href_within("addressbook-home-set", raw)
        if not href:
            self._addressbooks = []
            return self._addressbooks
        home = urljoin(principal, href)
        # 3. addressbook collections under the home (Depth 1).
        raw = self._propfind(home, "<d:resourcetype/>", "1")
        urls: list[str] = []
        for block in _RESPONSE_RE.findall(raw):
            if "addressbook" in block.lower():
                href = _HREF_RE.search(block)
                if href:
                    urls.append(urljoin(home, href.group(1).strip()))
        self._addressbooks = urls
        return urls

    def fetch_vcard_entries(self) -> list[tuple[str, str]]:
        """Return (resource_url, vcard_text) for every contact across all addressbooks."""
        report = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<c:addressbook-query xmlns:d="DAV:" xmlns:c="%s">'
            "<d:prop><d:getetag/><c:address-data/></d:prop></c:addressbook-query>" % _CARDDAV
        )
        entries: list[tuple[str, str]] = []
        for url in self._addressbook_urls():
            raw = self._client().request(
                url, "REPORT", report, {"Depth": "1", "Content-Type": "application/xml"}
            ).raw
            for block in _RESPONSE_RE.findall(raw):
                data = _ADDRDATA_RE.search(block)
                href = _HREF_RE.search(block)
                if data and href:
                    entries.append((urljoin(url, href.group(1).strip()), html.unescape(data.group(1)).strip()))
        return entries

    def fetch_vcards(self) -> list[str]:
        """Return raw vCard text for every contact (read path)."""
        return [text for _url, text in self.fetch_vcard_entries()]

    # --- write transport (mutation orchestration lives in writes.py) ---

    def addressbook_url(self) -> str:
        urls = self._addressbook_urls()
        if not urls:
            raise RuntimeError("No CardDAV addressbook found to write to.")
        return urls[0]

    def find_entry_by_uid(self, uid: str) -> tuple[str, str] | None:
        """Return (resource_url, vcard_text) for the contact with this UID, or None."""
        needle = f"UID:{uid}"
        for url, text in self.fetch_vcard_entries():
            if any(line.strip() == needle for line in text.splitlines()):
                return url, text
        return None

    def put_vcard(self, url: str, text: str) -> None:
        resp = self._client().request(
            url, "PUT", text, {"Content-Type": "text/vcard; charset=utf-8"}
        )
        if resp.status not in (200, 201, 204):
            raise RuntimeError(f"CardDAV PUT failed (HTTP {resp.status}).")

    def delete_resource(self, url: str) -> None:
        resp = self._client().request(url, "DELETE")
        if resp.status not in (200, 204):
            raise RuntimeError(f"CardDAV DELETE failed (HTTP {resp.status}).")

    def close(self) -> None:
        self._dav = None
        self._addressbooks = None


class CalendarClient:
    """Holds a CalDAV principal for the process lifetime."""

    def __init__(self, settings: Settings):
        self._settings = settings
        self._principal = None
        self.lock = threading.RLock()

    def principal(self):
        if self._principal is None:
            s = self._settings
            dav = caldav.DAVClient(
                url=s.caldav_url,
                username=s.username,
                password=get_password(s.keyring_service, s.username),
                timeout=NET_TIMEOUT,
            )
            self._principal = dav.principal()
        return self._principal

    def close(self) -> None:
        self._principal = None
