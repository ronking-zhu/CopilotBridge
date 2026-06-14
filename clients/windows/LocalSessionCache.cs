using System.IO;
using System.Text.Json;

namespace CopilotBridgeClient;

/// <summary>
/// Best-effort local mirror of server sessions under
/// <c>%APPDATA%\CopilotBridgeClient\sessions\&lt;id&gt;.json</c>. The server is
/// authoritative for the session list; this cache only exists so the sidebar and
/// transcripts render instantly on startup and survive brief offline periods.
/// Every method swallows IO/JSON errors so a broken cache never crashes the UI
/// (mirrors <see cref="AppSettings"/>'s defensive style).
/// </summary>
internal static class LocalSessionCache
{
    private static readonly JsonSerializerOptions JsonOpts =
        new() { WriteIndented = true, PropertyNameCaseInsensitive = true };

    private static string Dir => Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
        "CopilotBridgeClient", "sessions");

    /// <summary>Session ids come from the server (uuid) or "win-xxxx"; only allow
    /// filename-safe characters before touching the filesystem.</summary>
    private static bool IsSafeId(string? id) =>
        !string.IsNullOrEmpty(id) && id.All(c => char.IsLetterOrDigit(c) || c is '-' or '_');

    private static string FileFor(string id) => Path.Combine(Dir, id + ".json");

    /// <summary>Load every cached session (the directory listing IS the index).</summary>
    public static List<SessionDetail> LoadAll()
    {
        var list = new List<SessionDetail>();
        try
        {
            if (!Directory.Exists(Dir)) return list;
            foreach (var path in Directory.EnumerateFiles(Dir, "*.json"))
            {
                try
                {
                    var d = JsonSerializer.Deserialize<SessionDetail>(File.ReadAllText(path), JsonOpts);
                    if (d is not null && !string.IsNullOrEmpty(d.Id)) list.Add(d);
                }
                catch { /* skip a single corrupt file */ }
            }
        }
        catch { /* unreadable dir -> empty */ }
        return list;
    }

    public static SessionDetail? Load(string id)
    {
        try
        {
            if (!IsSafeId(id)) return null;
            var path = FileFor(id);
            if (!File.Exists(path)) return null;
            return JsonSerializer.Deserialize<SessionDetail>(File.ReadAllText(path), JsonOpts);
        }
        catch { return null; }
    }

    /// <summary>Write a full session (summary + messages).</summary>
    public static void Save(SessionDetail detail)
    {
        try
        {
            if (!IsSafeId(detail.Id)) return;
            Directory.CreateDirectory(Dir);
            File.WriteAllText(FileFor(detail.Id), JsonSerializer.Serialize(detail, JsonOpts));
        }
        catch { /* best-effort */ }
    }

    /// <summary>Update only the summary fields, preserving any cached messages.</summary>
    public static void SaveSummary(SessionSummary s)
    {
        try
        {
            if (!IsSafeId(s.Id)) return;
            var detail = Load(s.Id) ?? new SessionDetail();
            detail.Id = s.Id;
            detail.Title = s.Title;
            detail.CreatedAt = s.CreatedAt;
            detail.UpdatedAt = s.UpdatedAt;
            detail.MessageCount = s.MessageCount;
            Save(detail);
        }
        catch { /* best-effort */ }
    }

    public static void Delete(string id)
    {
        try
        {
            if (!IsSafeId(id)) return;
            var path = FileFor(id);
            if (File.Exists(path)) File.Delete(path);
        }
        catch { /* best-effort */ }
    }
}
