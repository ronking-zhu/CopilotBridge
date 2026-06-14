using System.IO;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace CopilotBridgeClient;

/// <summary>Persisted client settings. The API key is stored encrypted (DPAPI);
/// the plaintext key only lives in memory at runtime.</summary>
public class AppSettings
{
    public string ServerUrl { get; set; } = "http://localhost:3978";
    public string ApiKeyProtected { get; set; } = "";

    /// <summary>Id of the last-active server session (uuid). Replaces the old
    /// client-generated ConversationId; unknown fields in older settings.json are
    /// ignored by System.Text.Json, so loading stays backward-compatible.</summary>
    public string ActiveSessionId { get; set; } = "";

    [JsonIgnore] public string ApiKey { get; set; } = "";

    private static string Dir => Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "CopilotBridgeClient");

    private static string FilePath => Path.Combine(Dir, "settings.json");

    public static AppSettings Load()
    {
        try
        {
            if (File.Exists(FilePath))
            {
                var s = JsonSerializer.Deserialize<AppSettings>(File.ReadAllText(FilePath)) ?? new AppSettings();
                if (!string.IsNullOrEmpty(s.ApiKeyProtected))
                {
                    try { s.ApiKey = Dpapi.Unprotect(s.ApiKeyProtected); }
                    catch { s.ApiKey = ""; }
                }
                return s;
            }
        }
        catch { /* fall through to defaults */ }
        return new AppSettings();
    }

    public void Save()
    {
        try
        {
            Directory.CreateDirectory(Dir);
            try { ApiKeyProtected = string.IsNullOrEmpty(ApiKey) ? "" : Dpapi.Protect(ApiKey); }
            catch { ApiKeyProtected = ""; }

            File.WriteAllText(FilePath,
                JsonSerializer.Serialize(this, new JsonSerializerOptions { WriteIndented = true }));
        }
        catch { /* best-effort persistence */ }
    }
}
