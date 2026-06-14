# Android client (.NET MAUI)

Native Android client for Copilot Bridge, built with .NET 9 MAUI. Shares the same
server contract and behavior as the Windows client: a server-authoritative session
list, locally cached sessions, and the async `/api/chat` job flow.

## Build

The Android SDK must be discoverable. Set these once per shell (Android Studio's SDK):

```powershell
$env:ANDROID_HOME = "$env:LOCALAPPDATA\Android\Sdk"
$env:ANDROID_SDK_ROOT = $env:ANDROID_HOME

cd clients\android
dotnet build -c Debug -f net9.0-android        # framework build
dotnet publish -c Release -f net9.0-android     # Release APK
```

The Release APK is written under `clients\android\bin\Release\net9.0-android\`.

> Requires the `android` workload (`dotnet workload install android`), a JDK 17, and an
> installed Android platform. If a build reports `XA5207` (missing API level), run:
> `dotnet build -t:InstallAndroidDependencies -f net9.0-android -p:AcceptAndroidSDKLicenses=True`

## Using it

1. **Server URL** — defaults to `http://10.0.2.2:3978`, the Android emulator's alias for the
   host machine's `localhost`. On a real device, set the Dev Tunnel **https** URL.
2. **API Key** — the `CHAT_API_TOKEN` from `server/.env`. Stored in Android `SecureStorage`
   (Keystore-backed), never in plain text.
3. Tap **检测连接 / Check**; once connected, the session list loads. Tap **＋** to start a new
   conversation; tap a session to open it. Swipe a row to rename or delete.

## Networking

`Platforms/Android/Resources/xml/network_security_config.xml` permits cleartext HTTP only to
`10.0.2.2`/`localhost`/`127.0.0.1` (for the emulator→host default). All other hosts must use
HTTPS — so Dev Tunnel URLs work unchanged and cleartext to arbitrary hosts is blocked.

## Source map

| Path | Purpose |
| --- | --- |
| `Services/BridgeApiClient.cs` | HTTP client for `/health`, `/api/chat`, `/api/sessions` |
| `Services/LocalSessionCache.cs` | Local mirror under `FileSystem.AppDataDirectory/sessions/` |
| `Services/AppSettings.cs` | Server URL + active session (Preferences); API key (SecureStorage) |
| `Models/Models.cs` | DTOs + view models (matches server camelCase) |
| `Pages/SessionsPage.*` | Session list (CollectionView + swipe rename/delete) |
| `Pages/ChatPage.*` | Transcript + send/poll with live "thinking…" indicator |
| `Pages/SettingsPage.*` | Server URL + API key + connection check |

## Status

Implemented and building (framework + Release APK). On-device/emulator runtime testing over a
Dev Tunnel and signed-release packaging are follow-ups.
