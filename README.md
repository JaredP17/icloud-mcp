# icloud-mcp

<!-- mcp-name: io.github.eodozzy/icloud-mcp -->

[![PyPI](https://img.shields.io/pypi/v/icloud-mcp.svg)](https://pypi.org/project/icloud-mcp/)
[![Python](https://img.shields.io/pypi/pyversions/icloud-mcp.svg)](https://pypi.org/project/icloud-mcp/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A [Model Context Protocol](https://modelcontextprotocol.io) server that gives AI
assistants **read-only** access to your iCloud mail and calendar over IMAP and
CalDAV. Credentials live in your OS keyring (macOS Keychain by default) — never
in a config file or the repo.

Defaults target iCloud, but every endpoint is overridable, so it works against
any standard IMAP/CalDAV provider.

## Status

**Read-only by default; writes are strictly opt-in.** Mail, calendar, and contacts
can be listed, searched, and read with no ability to change anything: the IMAP
connection is opened read-only and bodies are fetched with `BODY.PEEK[]`, so
messages are never even marked as read.

Mutating tools span mail (`send_mail`, `reply_mail`, `forward_mail`), calendar
(`create_event`, `update_event`, `delete_event`), and contacts (`create_contact`,
`update_contact`, `delete_contact`) — but all are **disabled unless you set
`ICLOUD_ENABLE_WRITES=1`**, and even then every write requires an interactive
confirmation (MCP elicitation) before it runs. See [Writes (opt-in)](#writes-opt-in).

### Writes (opt-in)

Mutations are gated two ways:

1. **Operator switch** — write tools refuse with an explanatory error unless
   `ICLOUD_ENABLE_WRITES=1` is set in the server's environment.
2. **Per-action confirmation** — when enabled, each write tool calls back to the client
   to confirm the exact action before executing. Destructive operations (delete event /
   contact) are gated identically — no extra force flag, just the same confirmation.

iCloud issues a single app-specific password covering IMAP, SMTP, CalDAV, and CardDAV
— there is no scoped "write-only" credential — so write-safety is *structural*: mail is
sent over a separate, fresh SMTP connection (the read path's IMAP client has no send
capability), and the opt-in flag + confirmation guard every mutation.

## Install

This is a fork of [eodozzy/icloud-mcp](https://github.com/eodozzy/icloud-mcp) with
fixes not yet in the upstream PyPI release: SMTP certificate verification, safer
write timeouts, thread-safe connections, and dependency version limits (the PyPI
0.1.1 release pulls in `mcp` 2.x and fails on import). Install from this fork, not
with `pip install icloud-mcp`:

```bash
pip install git+https://github.com/JaredP17/icloud-mcp
```

Or run it without installing — handy for MCP clients that launch the server
on demand:

```bash
uvx --from git+https://github.com/JaredP17/icloud-mcp icloud-mcp
```

Or for local development:

```bash
git clone https://github.com/JaredP17/icloud-mcp
cd icloud-mcp
python3 -m venv .venv && .venv/bin/pip install -e ".[test]"
```

Requires Python 3.11+.

## Credentials

Generate an [app-specific password](https://appleid.apple.com) for your Apple ID
(never use your main password), then store it in your keyring:

```bash
# macOS
security add-generic-password -a "you@icloud.com" -s "icloud-mcp" -w "xxxx-xxxx-xxxx-xxxx"
```

On other platforms, the cross-platform [`keyring`](https://pypi.org/project/keyring/)
library is used — `keyring set icloud-mcp you@icloud.com` also works anywhere.

The username comes from the `ICLOUD_USERNAME` environment variable. Resolution
order for the password:

1. OS keyring (`ICLOUD_KEYRING_SERVICE`, default `icloud-mcp`, + username)
2. `ICLOUD_APP_PASSWORD` environment variable (fallback)

To **reuse an existing keyring entry** (e.g. one named `my-icloud`), set
`ICLOUD_KEYRING_SERVICE=my-icloud`.

## Configuration

Set `ICLOUD_USERNAME` (required). All else is optional — see
[`.env.example`](.env.example) for the full list of endpoint/timezone overrides.

## Register with an MCP client

The server speaks MCP over **stdio**: a client launches the `icloud-mcp` command
as a subprocess and talks to it over stdin/stdout. "Installing" it into a client
just means telling that client which command to run and which env vars to pass —
the password itself stays in the keyring, never in the client config.

If `icloud-mcp` isn't on your `PATH` (e.g. you installed into a virtualenv),
use the absolute path to the launcher, e.g. `/path/to/repo/.venv/bin/icloud-mcp`.

### Claude Code

```bash
claude mcp add icloud \
  --env ICLOUD_USERNAME=you@icloud.com \
  --env ICLOUD_KEYRING_SERVICE=icloud-mcp \
  -- icloud-mcp
```

Then `claude mcp list` to confirm. Add `-s user` to make it available across all
projects rather than just the current one.

### Claude Desktop

Add the server to Claude Desktop's config file. On macOS this is:

```
~/Library/Application Support/Claude/claude_desktop_config.json
```

(On Windows: `%APPDATA%\Claude\claude_desktop_config.json`.)

The easiest way to open it is **Settings → Developer → Edit Config**. Add an
`mcpServers` key alongside whatever is already in the file — do **not** paste a
second top-level `{ ... }` object, or the file becomes invalid JSON:

```json
{
  "mcpServers": {
    "icloud": {
      "command": "icloud-mcp",
      "env": {
        "ICLOUD_USERNAME": "you@icloud.com",
        "ICLOUD_KEYRING_SERVICE": "icloud-mcp"
      }
    }
  }
}
```

If the file already contains other keys, merge `mcpServers` in as a sibling
(remember the comma between keys):

```json
{
  "someExistingSetting": "...",
  "mcpServers": { "icloud": { "...": "..." } }
}
```

Then **fully quit** Claude Desktop (⌘Q, not just closing the window) and reopen
it. The server shows up as a *Local MCP server* and its tools become available.

Notes:

- **First call prompts for Keychain access.** macOS asks whether `icloud-mcp`
  may read the keyring item; click **Always Allow** to avoid repeat prompts.
- **Claude Desktop may rewrite this file** when it saves its own preferences,
  dropping hand-added keys it doesn't recognize. If the server disappears after
  you change other settings, just re-add the `mcpServers` block.

## Tools & resources

**Tools** (model-invoked):

| Tool | Description |
|---|---|
| `list_mail` | List a folder (default INBOX), newest first, with optional `since_date` |
| `search_mail` | Full-text search the inbox (or a named folder) |
| `list_folders` | List the available IMAP mailbox folder names |
| `get_message` | Fetch one message by UID, with full body |
| `list_events` | Calendar events in a date window |
| `search_events` | Events whose title matches text, in a window |
| `list_calendars` | Names of all calendars |
| `list_contacts` | List address-book contacts (name, emails, phones, org) |
| `search_contacts` | Find contacts matching text across name, org, and emails |
| `send_mail` ⚠️ | Send a plain-text email |
| `reply_mail` ⚠️ | Reply to a message by UID (quotes original; `reply_all` optional) |
| `forward_mail` ⚠️ | Forward a message by UID to a new recipient |
| `create_event` ⚠️ | Create a calendar event |
| `update_event` ⚠️ | Edit an event by UID (only the fields you pass) |
| `delete_event` ⚠️ | Delete an event by UID |
| `create_contact` ⚠️ | Create a new contact |
| `update_contact` ⚠️ | Edit a contact by UID (merges into the existing vCard) |
| `delete_contact` ⚠️ | Delete a contact by UID |

⚠️ Write tools require `ICLOUD_ENABLE_WRITES=1` and confirm each action — see
[Writes (opt-in)](#writes-opt-in).

**Resources** (passive context):

| URI | Description |
|---|---|
| `icloud://mail/inbox/recent` | Most recent inbox messages |
| `icloud://calendar/today` | Today + tomorrow's events |

## Behavior notes

- **Empty/missing headers:** messages whose `Subject` header is absent *or*
  present-but-blank render as `(no subject)`; a blank/absent `From` renders as
  `(unknown)`. (Some mail has an empty subject line rather than no subject line
  at all — both are normalized.)
- **Snippets** prefer the `text/plain` part; for HTML-only mail, tags are
  stripped (`<style>`/`<script>`/`<head>` content discarded) so you still get a
  readable preview.
- **Double-wrapped bodies:** some senders (e.g. USPS Informed Delivery) embed a
  redundant MIME header block at the top of the decoded body. A leading
  `Content-*`/`MIME-Version` header block is stripped so those headers don't leak
  into the snippet or body.
- **`since_date` is date-granular** and evaluated in the IMAP server's timezone
  (UTC for iCloud), so a `since_date` of today can include late-yesterday messages
  in your local time.

## Development

```bash
.venv/bin/pytest            # run the fixture-based test suite (no live account)
.venv/bin/mcp dev -m icloud_mcp.server   # interactive MCP Inspector
```

## Security notes

- The app-specific password lives in the OS keyring only — never in `.env`,
  never committed.
- Read access (mail, calendar, contacts) cannot mutate anything; the IMAP session
  is opened read-only.
- Writes are **off by default**. They require `ICLOUD_ENABLE_WRITES=1` *and* an
  interactive confirmation per action, and mail is sent over a separate SMTP
  connection isolated from the read path.
- An app-specific password can be revoked at any time from appleid.apple.com
  without affecting your Apple ID.

## License

MIT — see [LICENSE](LICENSE).

---

Not affiliated with, endorsed by, or sponsored by Apple Inc. iCloud is a
trademark of Apple Inc.
