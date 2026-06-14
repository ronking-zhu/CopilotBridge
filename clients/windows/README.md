# Windows client (WPF, .NET 9)

Native desktop chat client for the Copilot Bridge. Targets Windows 11 (x64 today,
ARM64 buildable).

## Build & run (dev)

```powershell
cd clients\windows
dotnet run -c Release
```

## Publish a single-file exe

From the repo root:

```powershell
.\scripts\publish-client.ps1                 # win-x64 (default)
.\scripts\publish-client.ps1 -Runtime win-arm64
```

Output: `clients\windows\publish\<runtime>\CopilotBridgeClient.exe` — self-contained,
no .NET install required on the target machine.

## Using it

1. **Server URL** — the Dev Tunnel address (e.g. `https://<id>-3978.<region>.devtunnels.ms`)
   for a remote machine, or `http://localhost:3978` on the host.
2. **API Key** — the `CHAT_API_TOKEN` value from `server/.env`.
3. Click **检测连接 / Check connection**; once green, chat. Enter sends, Shift+Enter newlines.

The API key is saved encrypted with Windows DPAPI under
`%APPDATA%\CopilotBridgeClient\settings.json`.

## Source map

| File | Purpose |
| --- | --- |
| `MainWindow.xaml(.cs)` | Chat UI + live polling/“thinking” timer |
| `BridgeApiClient.cs` | HTTP calls to `/health`, `/api/chat`, `/api/chat/{jobId}` |
| `Models.cs` / `Converters.cs` | View model + XAML converters |
| `AppSettings.cs` / `Dpapi.cs` | Settings persistence + DPAPI-encrypted key |
