# Architecture

## Components

```mermaid
flowchart TB
    subgraph host["Host machine"]
        L["launcher.py<br/>(orchestrator)"]
        A["app.py<br/>aiohttp server :3978"]
        W["webchat.py<br/>/api/chat job API"]
        B["bot.py<br/>Bot Framework /api/messages"]
        R["copilot_runner.py"]
        D["devtunnel.py"]
        CLI["GitHub Copilot CLI"]
        DT["devtunnel host<br/>(child process)"]
        L --> A
        L --> D --> DT
        A --> W
        A --> B
        W --> R --> CLI
        B --> R
    end
    Client["Clients / phone / HTTP"] -->|HTTPS| Relay["Dev Tunnel relay"] --> DT --> A
```

## Startup sequence (launcher.py)

The orchestrated launcher brings the server and tunnel up together and logs each stage:

1. **Configuration & app graph** — load `server/.env`, build the aiohttp app, wire the
   Copilot runner.
2. **Copilot CLI** — confirm the `copilot` executable is located.
3. **Dev Tunnel CLI** — discover `devtunnel` (bundled `tools/` → PATH → winget link).
4. **Bind + serve** — start listening on `HOST:PORT`.
5. **Readiness probe** — poll `/health` until it returns 200.
6. **Host the Dev Tunnel** — ensure the named tunnel + an **http** port mapping exist,
   then `devtunnel host` as a child process and capture the public URL.

On `Ctrl+C` the launcher stops the tunnel child process and cleanly shuts the server down.

The interactive installer starts the launcher with `--notification-setup`. After the local
server is ready, the launcher opens
`/?dashboard=1&notificationSetup=1&syncSetup=1` in the default browser. The Dashboard combines
browser-notification consent with explicit OneDrive account selection. Chromium requires
`Notification.requestPermission()` to follow a user gesture, and OneDrive authorization must
also remain an explicit user choice. An upgrade can open this onboarding page without rerunning
the full setup wizard.

## Why the tunnel port is `http`, not `https`

`devtunnel port create --protocol` describes the protocol the **local** service speaks.
The app server is plain HTTP, so the port must be `http`. Selecting `https` makes the relay
attempt a TLS handshake against the HTTP socket and every request fails with
**502 Bad Gateway** (the server logs a `BadStatusLine` on a TLS ClientHello). The public
tunnel URL is HTTPS either way — the relay terminates TLS.

## Sessions (server-authoritative, persistent)

`server/session_store.py` owns the local SQLite database at
`server/sessions/copilotbridge.db` (or the equivalent writable app-data directory in a
packaged build). WAL mode permits concurrent readers while one serialized writer updates
the store.

- **Identity:** the client `conversationId` is a Bridge-owned UUID. Device-scoped
  `external_refs` identify imported CLI/VS Code sessions; `execution_bindings` map a Branch
  to the provider-native id used to run it. These id namespaces are never interchangeable.
- **Native discovery:** CLI, VS Code Stable, and VS Code Insiders records use source-qualified
  keys, so identical native UUIDs do not overwrite each other. VS Code transcript rows come
  from `github.copilot-chat/session-store.db`; pinned titles and change statistics come from
  the Workbench `state.vscdb` entry `chat.ChatSessionStore.index`.
- **Conversation structure:** every user request creates a stable Turn UUID. User, assistant,
  and future tool messages point to that Turn, enabling prompt previews and message-level
  deep links without relying on array positions.
- **Legacy migration:** existing `sessions/<id>.json` files are imported idempotently and
  retained as rollback backups. Imported records and their native source references are
  added to the transactional sync outbox.
- **Flow:** `webchat.py` resolves the Bridge conversation, captures prior history, appends the
  user message, runs the provider through its local execution binding, then appends the reply.
- **New conversation:** `POST /api/sessions` mints a fresh uuid. `reset:true` on `/api/chat`
  also mints a new session and returns its id for the client to adopt (no reset-in-place).
- **Client sync:** clients list sessions from `GET /api/sessions` (authoritative) and may cache
  transcripts locally, reconciling against the server (server wins for existence + ordering).

## Prompt timeline

`GET /api/sessions/{id}/turns` returns one preview per user Turn. The preview is projected
from the complete message at query time; `promptPreviewLength` defaults to 200 and is changed
through `GET/PATCH /api/settings`. The PWA deep-links to
`?conversation=<id>&turn=<turn-id>` and scrolls to the exact user/assistant exchange.

## Session watcher and unified inbox

`server/session_watcher.py` polls the read-only Copilot CLI, VS Code, and VS Code
Insiders adapters. The first observation establishes a cursor without creating historical
alerts. Later assistant responses must be reported complete by the source and remain stable
for `SESSION_WATCHER_SETTLE_SCANS` scans before one deduplicated `inbox_items` row is created.

- Copilot CLI completion is derived from `assistant.turn_start` / `assistant.turn_end` and
  pending permission events in `events.jsonl`.
- VS Code completion uses the persisted `ResponseModelState` in
  `chat.ChatSessionStore.index` (`Pending`, `Complete`, `Cancelled`, `Failed`, `NeedsInput`).
- Bridge-owned asynchronous jobs write to the same inbox immediately on completion or error.
- Inbox rows and per-source watch cursors live in SQLite, so restarts do not duplicate or lose
  attention state.

The PWA's AI Dashboard shows unread replies, active Bridge jobs, watched session count, source
labels, manual scan, and read/ignored states. It polls every five seconds and can emit browser
notifications while the PWA is open in the foreground or background. Without a cloud push
service, a fully closed browser cannot receive a new notification; the persisted inbox appears
the next time it opens.

Clicking an inbox item opens or imports the corresponding Bridge conversation. Stable VS Code
APIs do not provide a supported external deep link to an existing Copilot Chat turn, so exact
activation inside VS Code remains a future Companion Extension capability.

## Modular synchronization foundation

Every local mutation and its `sync_events` outbox row commit in the same SQLite transaction.
`server/sync/` encodes ordered events into versioned, gzip-compressed, SHA-256-verified,
immutable `.cbe` packages. `server/transports/` exposes a provider-neutral object contract:
list by prefix, put-if-absent, get, exists, and diagnostics.

`FileSystemTransport` supports local directories and mounted SMB/Samba shares.
`OneDriveGraphTransport` implements the same contract through Microsoft Graph's App Folder;
S3, SFTP, or other providers can be added without changing the sync engine or conversation
model. Each device writes only its own path under
`spaces/<space>/v1/devices/<device>/events/`. Pull applies events idempotently and advances a
per-device cursor in the same transaction without writing those remote changes back to the
local outbox.

### First sync when Laptop B already has history

`SyncCoordinator.first_sync()` always performs this deterministic sequence:

1. Read CLI, VS Code, and VS Code Insiders stores without modifying them.
2. Sort source-qualified native ids and import any missing local sessions idempotently.
3. Drain B's transactional Outbox completely into B-owned immutable packages.
4. List, verify, and apply packages previously uploaded by A and other devices.
5. Preserve remote-origin native ids as device-scoped references; do not treat them as locally
  resumable sessions.

Thus first installation computes $A \cup B$ rather than choosing a winner. Repeating the whole
sequence is a no-op. `SyncService` uses this same path immediately after authorization, at
startup when a cached account exists, on manual request, and every 15 minutes by default.

The native Tk Control Panel calls the same localhost-only sync API and never handles Graph
tokens itself. It displays account, pending-event, and last-result state and exposes Open AI
Dashboard, Connect OneDrive, Upload + download now, and Disconnect. Network and process work
runs on worker threads; only the queue-driven UI pump mutates Tk widgets.

OneDrive authentication uses MSAL Authorization Code + PKCE with delegated
`Files.ReadWrite.AppFolder`. The serialized MSAL refresh-token cache is encrypted with Windows
DPAPI under the current user and stored outside SQLite. The app registration is a public client
and contains no client secret.

`ONEDRIVE_TRANSPORT=auto` selects Graph only when a publisher-owned Client ID is configured.
Otherwise Windows work/personal OneDrive roots are detected from supported environment and
account registry metadata, and `FileSystemTransport` writes the same immutable object tree to
`<OneDrive>\Apps\Copilot Bridge`. Local-folder mode persists a separate explicit opt-in marker;
detecting a signed-in OneDrive never silently uploads data. This fallback avoids treating an
unrelated tenant-local test registration as a multi-tenant production application.

## Image attachments

Clients can attach images to a turn (`images:[{name, mime, data}]` on `/api/chat` or
`/api/chat-sync`; `data` is base64 or a `data:` URL). `webchat.py` base64-decodes and validates
each (type allow-list + size), saves it to `server/workspace/.uploads/<sessionId>/` (inside the
Copilot `--add-dir` sandbox), and passes the file path to `copilot_runner.py`, which adds one
`--attachment <path>` argument per image — the CLI's native way to give the model vision. The
saved files are recorded on the user message as `attachments[]` (name/mime/url/size) and served
read-only via `GET /api/uploads/{sid}/{file}`. Legacy `apikey` mode also accepts a `?key=` query
token so plain `<img>` tags (which can't set headers) work. Caps: `MAX_ATTACHMENT_MB`,
`MAX_ATTACHMENTS`.

## Auth & security model

- **Local web/API access** binds to loopback and requires no login in default `tunnel` mode.
- **Remote web/API access** crosses a private Dev Tunnel whose relay requires the tunnel owner's
  Microsoft account before forwarding any request.
- **Control API access** remains gated by `CHAT_API_TOKEN`; that secret is not injected into web pages.
- **`/api/messages`** (Bot Framework/Teams) validates the inbound JWT when `MicrosoftAppId`
  is set, and restricts users via `ALLOWED_USER_IDS`.
- **Copilot sandbox** — `COPILOT_SCOPE=workdir` confines file access to `server/workspace/`.
  `full` grants whole-machine access (`--allow-all`); use deliberately.
- **Fail-closed fallback** — `AUTH_MODE=tunnel` falls back to API-key authentication when `HOST`
  is non-loopback or `TUNNEL_AUTH=anonymous`.

## Configuration (server/.env)

See [server/.env.example](../server/.env.example) for the full list. Key settings:

| Var | Meaning |
| --- | --- |
| `AUTH_MODE` | `tunnel` (default), `apikey`, `entra`, or `both`. |
| `CHAT_API_TOKEN` | Host-only control secret; also protects legacy `apikey` mode. |
| `MAX_ATTACHMENT_MB` | Per-image upload size cap (MB, default 25). |
| `MAX_ATTACHMENTS` | Max images per chat turn (default 8). |
| `COPILOT_SCOPE` | `workdir` (sandbox) or `full`. |
| `TUNNEL_ENABLED` | Auto-start the Dev Tunnel from the launcher. |
| `TUNNEL_ID` | Stable tunnel name → stable public URL. |
| `TUNNEL_AUTH` | `private` (default), `tenant`, `anonymous`, or `org:<name>`. |
| `DEVTUNNEL_PATH` | Override devtunnel discovery. |
| `SESSION_WATCHER_ENABLED` | Enable native CLI/VS Code response monitoring. |
| `SESSION_WATCHER_INTERVAL` | Poll interval in seconds (default 5). |
| `SESSION_WATCHER_SETTLE_SCANS` | Unchanged scans required before alerting (default 1). |
| `ONEDRIVE_SYNC_ENABLED` | Enable the OneDrive sync service. |
| `ONEDRIVE_TRANSPORT` | `auto`, `graph`, or `local-folder`. |
| `ONEDRIVE_CLIENT_ID` | Publisher-owned multi-tenant Public Client id. |
| `ONEDRIVE_TENANT_ID` | OAuth authority tenant (`common` by default). |
| `ONEDRIVE_LOCAL_ROOT` | Optional explicit local OneDrive root. |
| `ONEDRIVE_SYNC_SPACE_ID` | Logical sync namespace inside the App Folder. |
| `ONEDRIVE_SYNC_INTERVAL` | Background interval in seconds (default 900). |
