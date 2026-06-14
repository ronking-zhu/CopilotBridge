# Releasing

How we version and ship Copilot Bridge. The product is a monorepo (server + clients),
so we use **one product-wide version** per release and attach a built asset for each
platform to that release.

## Versioning

- **Scheme:** [Semantic Versioning](https://semver.org/) — `vMAJOR.MINOR.PATCH`.
  - **MAJOR** — breaking API/protocol changes between client and server.
  - **MINOR** — backward-compatible features (e.g. the session list).
  - **PATCH** — backward-compatible fixes only.
- One git tag per release: `vX.Y.Z` (annotated). The tag is the source of truth; a GitHub
  Release is created from it.
- Keep `clients/windows/CopilotBridgeClient.csproj` `<Version>` in step with the release.

## Release artifact naming

Platform/architecture lives in the **asset file name**, never in the tag, so a single
release can carry every platform:

```
CopilotBridgeClient-<X.Y.Z>-<platform>-<arch>.<ext>
```

| Platform | Asset name (example for 1.0.0) | Status |
| --- | --- | --- |
| Windows x64 | `CopilotBridgeClient-1.0.0-win-x64.zip` | shipping |
| Windows ARM64 | `CopilotBridgeClient-1.0.0-win-arm64.zip` | planned |
| Android | `CopilotBridgeClient-1.0.0-android.apk` | planned |
| iOS | `CopilotBridgeClient-1.0.0-ios.ipa` | planned |

## Cutting a release

```powershell
# 1. Pick the version and make sure main is clean + pushed.
$ver = "1.0.0"

# 2. Build the platform asset(s).
.\scripts\publish-client.ps1 -Runtime win-x64
# (later) .\scripts\publish-client.ps1 -Runtime win-arm64

# 3. Package each asset with the versioned name into dist/ (dist/ is git-ignored).
New-Item -ItemType Directory -Force dist | Out-Null
Compress-Archive -Force `
  -Path clients\windows\publish\win-x64\CopilotBridgeClient.exe `
  -DestinationPath "dist\CopilotBridgeClient-$ver-win-x64.zip"

# 4. Tag and create the GitHub release (private repo) with the asset(s).
git tag -a "v$ver" -m "Copilot Bridge v$ver"
git push origin "v$ver"
gh release create "v$ver" "dist\CopilotBridgeClient-$ver-win-x64.zip" `
  --title "Copilot Bridge v$ver" --notes-file <notes>
```

## Adding a platform asset to an existing release

```powershell
gh release upload "v$ver" "dist\CopilotBridgeClient-$ver-win-arm64.zip"
```

## Server installer (single setup .exe)

The app server ships as a one-file Windows installer built with Inno Setup:

```powershell
# Builds the self-contained server (onedir) then compiles the installer.
.\scripts\build-installer.ps1 -Version 1.2.0
# -> dist\CopilotBridgeServer-1.2.0-setup.exe
```

Upgrade / uninstall behaviour (installer/CopilotBridgeServer.iss):

- **Upgrade in place** — every version shares the same `AppId`, so installing a
  newer build (e.g. 1.3.0) overwrites the existing install and updates the single
  "Programs and Features" entry instead of creating a second one. `[InstallDelete]`
  wipes the old `_internal\` payload first so removed files don't linger, and the
  running server/tunnel are stopped (`taskkill` + `CloseApplications`) so locked
  files can be replaced. Installing an **older** version over a newer one is blocked
  with a confirmation.
- **Uninstall from Control Panel** — the installer registers a normal uninstaller;
  uninstalling stops the server/tunnel, removes the program folder, and strips the
  install dir from the system PATH. User data in `%LOCALAPPDATA%\CopilotBridge`
  (the generated API key, sessions) is intentionally left in place.
- **Bump the version** by passing a new `-Version` to `build-installer.ps1`; tag and
  release the resulting `*-setup.exe` like any other asset.

