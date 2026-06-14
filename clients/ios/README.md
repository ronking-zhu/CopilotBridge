# iOS client

The iPhone/iPad app is the **same .NET MAUI app** as Android — it shares 100% of the
UI and logic (`Pages/`, `Services/`, `Models/`, `Markdown/`) and only adds the iOS
platform head. The project lives in [../android/CopilotBridgeAndroid.csproj](../android/CopilotBridgeAndroid.csproj),
which multi-targets `net9.0-android;net9.0-ios`.

## What's implemented

Identical feature set to the Windows/Android clients:

- Sessions list (sync from server) + create / open / rename / delete.
- Chat with async job flow: `POST /api/chat` then poll `GET /api/chat/{jobId}`,
  sending `X-API-Key` and `X-Tunnel-Skip-AntiPhishing-Page: true`.
- **Download conversation history** (`GET /api/sessions/{id}`) and resume a session.
- **Server-side queue display** ("排队中… 前面还有 N 个请求") when multiple clients
  share a session.
- Markdown-rendered assistant replies.
- API key stored in the iOS **Keychain** via `SecureStorage`.

### iOS-specific bits (in `../android/Platforms/iOS/`)

- `AppDelegate.cs`, `Program.cs` — standard MAUI iOS entry points.
- `Info.plist` — App Transport Security with `NSAllowsLocalNetworking` so
  `http://localhost:3978` works in the Simulator while the Dev Tunnel `https` URL is
  used over TLS on a real device.
- `Entitlements.plist` — placeholder for capabilities (e.g. Keychain groups, push).
- Default `ServerUrl` on iOS is `http://localhost:3978` (Simulator reaches the Mac
  host's localhost directly), vs `http://10.0.2.2:3978` on the Android emulator.

## Building & running (requires a Mac with Xcode)

Apple's toolchain only runs on macOS, so producing a runnable `.app`/`.ipa` requires
a Mac. The C# **compiles on Windows** (validated: `dotnet build -f net9.0-ios`
succeeds), but final linking/packaging needs Xcode.

**Option A — on a Mac directly:**

```bash
cd clients/android
# Simulator (no signing needed):
dotnet build -t:Run -c Debug -f net9.0-ios

# Real device / .ipa (needs an Apple Developer signing identity):
dotnet publish -c Release -f net9.0-ios \
  -p:RuntimeIdentifier=ios-arm64 \
  -p:CodesignKey="Apple Development: you@example.com" \
  -p:CodesignProvision="Your Provisioning Profile"
```

**Option B — from Windows via Visual Studio "Pair to Mac":**
Connect VS to a Mac over SSH, then select an iOS Simulator/device target and run.

## TestFlight via GitHub Actions (no Mac required) ⭐

The repo ships a ready-to-run pipeline,
[../../.github/workflows/ios-testflight.yml](../../.github/workflows/ios-testflight.yml),
that builds + signs the `.ipa` on a **macOS cloud runner** and uploads it to
TestFlight. The build is `dotnet publish -f net9.0-ios … -p:ArchiveOnBuild=true`
and the upload uses Fastlane `upload_to_testflight` with an App Store Connect API
key — see [../android/fastlane/Fastfile](../android/fastlane/Fastfile).

### One-time prerequisites (only you can do these)

1. **Apple Developer Program** membership (paid, $99/yr) — required for TestFlight.
2. In your Apple Developer account → **Certificates, Identifiers & Profiles**:
   - Register an **App ID** with the explicit bundle id `com.copilotbridge.client`.
   - Create an **Apple Distribution** certificate, then export it (with its private
     key) from Keychain Access as a `.p12` (note the password you set).
   - Create an **App Store** provisioning profile for that App ID → download the
     `.mobileprovision`.
3. In **App Store Connect**:
   - Create the **app record** for `com.copilotbridge.client` (name, etc.).
   - Users and Access → **Integrations → App Store Connect API** → generate a key
     with *App Manager* role. Download the `AuthKey_XXX.p8` and note its **Key ID**
     and **Issuer ID**.

### Wire up the secrets (from Windows)

With the [GitHub CLI](https://cli.github.com) authenticated (`gh auth login`):

```powershell
./scripts/ios-prepare-secrets.ps1 `
  -DistCertP12 C:\keys\dist.p12 -DistCertPassword 'your-p12-password' `
  -ProvisioningProfile C:\keys\AppStore.mobileprovision `
  -AscApiKeyP8 C:\keys\AuthKey_ABC123.p8 -AscKeyId ABC123 `
  -AscIssuerId 11111111-2222-3333-4444-555555555555
```

That base64-encodes the assets and creates all seven repo secrets the workflow
needs (cert, cert password, profile, a generated keychain password, API key, key
id, issuer id). The `.p12`/`.p8`/`.mobileprovision` files stay on your machine and
are git-ignored.

### Ship a build

- **Actions** tab → **iOS TestFlight** → **Run workflow** (optionally type the
  "What to Test" notes), **or**
- push a tag: `git tag ios-v1.1.2 && git push origin ios-v1.1.2`.

The runner signs the app, uploads it, and the build appears in TestFlight after
Apple finishes processing (a few minutes). Each run uses the GitHub run number as
the build number (`CFBundleVersion`) so uploads never collide. Add testers in
App Store Connect → TestFlight.

## Notes

- The app declares `ITSAppUsesNonExemptEncryption=false` (standard HTTPS only), so
  TestFlight won't block testers on the export-compliance question.
- iPhone users can also use the mobile web app served at the tunnel root
  (`https://<id>-3978.<region>.devtunnels.ms/`) without installing anything.
- Bundle id: `com.copilotbridge.client` (shared with Android). To ship both to the
  same account this is fine; change it if you need distinct store listings.
