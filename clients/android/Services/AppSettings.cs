using Microsoft.Maui.Storage;

namespace CopilotBridgeAndroid.Services;

/// <summary>Persisted client settings. On Android the API key lives in
/// <see cref="SecureStorage"/> (Android Keystore-backed, never plaintext); the
/// non-secret ServerUrl and ActiveSessionId live in <see cref="Preferences"/>.
/// The plaintext key only exists in memory at runtime via <see cref="ApiKey"/>.</summary>
public class AppSettings
{
    /// <summary>Default targets the host's localhost as seen from each emulator:
    /// Android maps the host to 10.0.2.2, while the iOS Simulator reaches the Mac
    /// host's localhost directly. Real devices should use the Dev Tunnel HTTPS URL.</summary>
#if IOS
    public string ServerUrl { get; set; } = "http://localhost:3978";
#else
    public string ServerUrl { get; set; } = "http://10.0.2.2:3978";
#endif

    /// <summary>Plaintext API key (in-memory only; persisted via SecureStorage).</summary>
    public string ApiKey { get; set; } = "";

    /// <summary>Id of the last-active server session (uuid).</summary>
    public string ActiveSessionId { get; set; } = "";

    private const string KeyServerUrl = "server_url";
    private const string KeyActiveSession = "active_session_id";
    private const string KeySecureApiKey = "api_key";
    // Fallback location for the API key when SecureStorage is unavailable (some
    // emulators / locked keystores). Base64 keeps it out of plain sight in the
    // Preferences XML; it is NOT strong encryption — SecureStorage is preferred.
    private const string KeyFallbackApiKey = "api_key_fallback";

    public static async Task<AppSettings> LoadAsync()
    {
        var s = new AppSettings();
        try { s.ServerUrl = Preferences.Get(KeyServerUrl, s.ServerUrl); } catch { /* defaults */ }
        try { s.ActiveSessionId = Preferences.Get(KeyActiveSession, ""); } catch { /* defaults */ }

        // Prefer SecureStorage; if it throws or has nothing, fall back to the
        // Preferences copy so the key survives a restart on emulators where the
        // keystore is unavailable. Never crash on a read failure.
        string key = "";
        try { key = await SecureStorage.GetAsync(KeySecureApiKey) ?? ""; }
        catch { key = ""; }
        if (string.IsNullOrEmpty(key))
        {
            try
            {
                var b64 = Preferences.Get(KeyFallbackApiKey, "");
                if (!string.IsNullOrEmpty(b64))
                    key = System.Text.Encoding.UTF8.GetString(Convert.FromBase64String(b64));
            }
            catch { /* ignore bad fallback */ }
        }
        s.ApiKey = key;
        return s;
    }

    public async Task SaveAsync()
    {
        try { Preferences.Set(KeyServerUrl, ServerUrl ?? ""); } catch { /* best-effort */ }
        try { Preferences.Set(KeyActiveSession, ActiveSessionId ?? ""); } catch { /* best-effort */ }

        bool secureOk = false;
        try
        {
            if (string.IsNullOrEmpty(ApiKey)) SecureStorage.Remove(KeySecureApiKey);
            else { await SecureStorage.SetAsync(KeySecureApiKey, ApiKey); secureOk = true; }
        }
        catch { secureOk = false; }

        // If SecureStorage worked, drop any stale fallback. Otherwise persist a
        // base64 fallback so the key isn't lost across restarts.
        try
        {
            if (secureOk || string.IsNullOrEmpty(ApiKey))
            {
                Preferences.Remove(KeyFallbackApiKey);
            }
            else
            {
                Preferences.Set(KeyFallbackApiKey,
                    Convert.ToBase64String(System.Text.Encoding.UTF8.GetBytes(ApiKey)));
            }
        }
        catch { /* best-effort */ }
    }
}
