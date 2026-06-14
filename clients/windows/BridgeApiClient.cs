using System.Linq;
using System.Net;
using System.Net.Http;
using System.Text;
using System.Text.Json;

namespace CopilotBridgeClient;

public class ApiException : Exception
{
    public HttpStatusCode? StatusCode { get; }
    public ApiException(string message, HttpStatusCode? code = null) : base(message) => StatusCode = code;
}

/// <summary>Talks to the Copilot bridge app server: /health and the async
/// /api/chat + /api/chat/{jobId} job API. Always sends the dev-tunnel
/// anti-phishing bypass header and the optional X-API-Key.</summary>
public class BridgeApiClient
{
    private static readonly JsonSerializerOptions JsonOpts = new() { PropertyNameCaseInsensitive = true };
    private readonly HttpClient _http;

    public string BaseUrl { get; set; } = "http://localhost:3978";
    public string ApiKey { get; set; } = "";

    public BridgeApiClient()
    {
        // No client-side timeout: long AI tasks can run for hours. The chat flow
        // uses the async job API (POST /api/chat then poll), so requests are short
        // anyway, but we disable the cap so nothing is ever cut off mid-task.
        _http = new HttpClient { Timeout = System.Threading.Timeout.InfiniteTimeSpan };
    }

    private HttpRequestMessage Build(HttpMethod method, string path, object? body = null)
    {
        var req = new HttpRequestMessage(method, Combine(BaseUrl, path));
        req.Headers.TryAddWithoutValidation("X-Tunnel-Skip-AntiPhishing-Page", "true");
        if (!string.IsNullOrEmpty(ApiKey))
            req.Headers.TryAddWithoutValidation("X-API-Key", ApiKey);
        if (body is not null)
            req.Content = new StringContent(JsonSerializer.Serialize(body), Encoding.UTF8, "application/json");
        return req;
    }

    public async Task<HealthInfo> HealthAsync(CancellationToken ct = default)
    {
        using var resp = await _http.SendAsync(Build(HttpMethod.Get, "/health"), ct);
        string text = await resp.Content.ReadAsStringAsync(ct);
        if (!resp.IsSuccessStatusCode)
            throw new ApiException($"HTTP {(int)resp.StatusCode}", resp.StatusCode);
        return JsonSerializer.Deserialize<HealthInfo>(text, JsonOpts) ?? new HealthInfo();
    }

    public async Task<StartJob> StartChatAsync(string message, string conversationId, bool reset,
        IReadOnlyList<OutgoingImage>? images = null, CancellationToken ct = default)
    {
        object payload = (images is { Count: > 0 })
            ? new
            {
                message, conversationId, reset,
                images = images.Select(i => new { name = i.Name, mime = i.Mime, data = i.Data }).ToArray(),
            }
            : new { message, conversationId, reset };
        using var resp = await _http.SendAsync(Build(HttpMethod.Post, "/api/chat", payload), ct);
        string text = await resp.Content.ReadAsStringAsync(ct);
        if (resp.StatusCode == HttpStatusCode.Unauthorized)
            throw new ApiException("unauthorized", HttpStatusCode.Unauthorized);
        if (!resp.IsSuccessStatusCode)
            throw new ApiException($"HTTP {(int)resp.StatusCode}: {text}", resp.StatusCode);
        return JsonSerializer.Deserialize<StartJob>(text, JsonOpts) ?? new StartJob();
    }

    /// <summary>GET an uploaded attachment's raw bytes (e.g. an image) for display.</summary>
    public async Task<byte[]> GetAttachmentBytesAsync(string url, CancellationToken ct = default)
    {
        using var resp = await _http.SendAsync(Build(HttpMethod.Get, url), ct);
        if (!resp.IsSuccessStatusCode)
            throw new ApiException($"HTTP {(int)resp.StatusCode}", resp.StatusCode);
        return await resp.Content.ReadAsByteArrayAsync(ct);
    }

    public async Task<ChatStatus> GetChatStatusAsync(string jobId, CancellationToken ct = default)
    {
        using var resp = await _http.SendAsync(Build(HttpMethod.Get, $"/api/chat/{jobId}"), ct);
        string text = await resp.Content.ReadAsStringAsync(ct);
        if (!resp.IsSuccessStatusCode)
            throw new ApiException($"HTTP {(int)resp.StatusCode}: {text}", resp.StatusCode);
        return JsonSerializer.Deserialize<ChatStatus>(text, JsonOpts) ?? new ChatStatus();
    }

    // ----- Session management (/api/sessions) -----

    /// <summary>GET /api/sessions -> summaries sorted updatedAt DESC (no messages).</summary>
    public async Task<List<SessionSummary>> ListSessionsAsync(CancellationToken ct = default)
    {
        using var resp = await _http.SendAsync(Build(HttpMethod.Get, "/api/sessions"), ct);
        string text = await resp.Content.ReadAsStringAsync(ct);
        if (resp.StatusCode == HttpStatusCode.Unauthorized)
            throw new ApiException("unauthorized", HttpStatusCode.Unauthorized);
        if (!resp.IsSuccessStatusCode)
            throw new ApiException($"HTTP {(int)resp.StatusCode}: {text}", resp.StatusCode);
        return JsonSerializer.Deserialize<List<SessionSummary>>(text, JsonOpts) ?? new();
    }

    /// <summary>POST /api/sessions -> the new session summary (its id is the conversationId).</summary>
    public async Task<SessionSummary> CreateSessionAsync(string title = "", CancellationToken ct = default)
    {
        using var resp = await _http.SendAsync(Build(HttpMethod.Post, "/api/sessions", new { title }), ct);
        string text = await resp.Content.ReadAsStringAsync(ct);
        if (resp.StatusCode == HttpStatusCode.Unauthorized)
            throw new ApiException("unauthorized", HttpStatusCode.Unauthorized);
        if (!resp.IsSuccessStatusCode)
            throw new ApiException($"HTTP {(int)resp.StatusCode}: {text}", resp.StatusCode);
        return JsonSerializer.Deserialize<SessionSummary>(text, JsonOpts)
               ?? throw new ApiException("server returned no session");
    }

    /// <summary>GET /api/sessions/{id} -> full session, or null on 404.</summary>
    public async Task<SessionDetail?> GetSessionAsync(string id, CancellationToken ct = default)
    {
        using var resp = await _http.SendAsync(
            Build(HttpMethod.Get, $"/api/sessions/{Uri.EscapeDataString(id)}"), ct);
        if (resp.StatusCode == HttpStatusCode.NotFound) return null;
        string text = await resp.Content.ReadAsStringAsync(ct);
        if (resp.StatusCode == HttpStatusCode.Unauthorized)
            throw new ApiException("unauthorized", HttpStatusCode.Unauthorized);
        if (!resp.IsSuccessStatusCode)
            throw new ApiException($"HTTP {(int)resp.StatusCode}: {text}", resp.StatusCode);
        return JsonSerializer.Deserialize<SessionDetail>(text, JsonOpts);
    }

    /// <summary>DELETE /api/sessions/{id} -> true if deleted, false on 404.</summary>
    public async Task<bool> DeleteSessionAsync(string id, CancellationToken ct = default)
    {
        using var resp = await _http.SendAsync(
            Build(HttpMethod.Delete, $"/api/sessions/{Uri.EscapeDataString(id)}"), ct);
        if (resp.StatusCode == HttpStatusCode.NotFound) return false;
        if (resp.StatusCode == HttpStatusCode.Unauthorized)
            throw new ApiException("unauthorized", HttpStatusCode.Unauthorized);
        if (!resp.IsSuccessStatusCode)
        {
            string text = await resp.Content.ReadAsStringAsync(ct);
            throw new ApiException($"HTTP {(int)resp.StatusCode}: {text}", resp.StatusCode);
        }
        return true;
    }

    /// <summary>PATCH /api/sessions/{id} {title} -> updated summary, or null on 404.</summary>
    public async Task<SessionSummary?> RenameSessionAsync(string id, string title, CancellationToken ct = default)
    {
        using var resp = await _http.SendAsync(
            Build(HttpMethod.Patch, $"/api/sessions/{Uri.EscapeDataString(id)}", new { title }), ct);
        if (resp.StatusCode == HttpStatusCode.NotFound) return null;
        string text = await resp.Content.ReadAsStringAsync(ct);
        if (resp.StatusCode == HttpStatusCode.Unauthorized)
            throw new ApiException("unauthorized", HttpStatusCode.Unauthorized);
        if (!resp.IsSuccessStatusCode)
            throw new ApiException($"HTTP {(int)resp.StatusCode}: {text}", resp.StatusCode);
        return JsonSerializer.Deserialize<SessionSummary>(text, JsonOpts);
    }

    private static string Combine(string baseUrl, string path)
        => baseUrl.TrimEnd('/') + "/" + path.TrimStart('/');
}
