# Clients

End-user apps that talk to the Copilot Bridge app server over the HTTP API
(`/api/chat`, `/api/chat/{jobId}`) through a public Dev Tunnel.

## Platform status

| Platform | Folder | Tech | Status |
| --- | --- | --- | --- |
| Windows x64 | [`windows/`](windows/) | WPF (.NET 9) | ✅ Shipping |
| Windows ARM64 | `windows/` (same project) | WPF (.NET 9) | 🛠️ Buildable via `-r win-arm64`, needs polish/testing |
| Android | [`android/`](android/) | .NET MAUI (net9.0-android) | ✅ Implemented (builds + Release APK); on-device testing pending |
| iOS | [`ios/`](ios/) | TBD (.NET MAUI or SwiftUI) | 🔜 Planned |

## Shared API contract

Every client implements the same flow against the server:

1. `POST /api/chat` with `{ "message", "conversationId", "reset" }` → returns `{ "jobId" }`.
2. Poll `GET /api/chat/{jobId}` until `status == "done"` → `{ "ok", "reply", "exitCode" }`.

Required headers:

- `X-API-Key: <CHAT_API_TOKEN>` — shared secret from `server/.env`.
- `X-Tunnel-Skip-AntiPhishing-Page: true` — bypasses the Dev Tunnels interstitial for
  non-browser HTTP clients.

Keep the same `conversationId` across calls to preserve Copilot's multi-turn context.

## Sessions (server-authoritative)

The `conversationId` is a server-owned session uuid that equals the Copilot `--session-id`.
Clients maintain a **session list** synced from the server and may cache it locally:

- `POST /api/sessions {title?}` → `{id,...}` — start a new conversation (get a fresh id).
- `GET /api/sessions` → `[summaries]` — the authoritative list (newest first).
- `GET /api/sessions/{id}` → full transcript (`messages[]`) to render history.
- `DELETE /api/sessions/{id}` / `PATCH /api/sessions/{id} {title}` — delete / rename.

The server is the source of truth for the list; a client cache should reconcile against
`GET /api/sessions` (add new, update titles/order, drop ids the server no longer has).

## Design guidance for new clients

- Show a live "thinking…" indicator while polling (Copilot runs can take many seconds).
- Store the API key in the platform secret store (Keychain / Keystore / DPAPI), never plain text.
- Make the server URL and key configurable; default the URL to the Dev Tunnel address.
