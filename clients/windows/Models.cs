using System.Collections.ObjectModel;
using System.ComponentModel;
using System.Text.Json.Serialization;
using System.Windows.Media;

namespace CopilotBridgeClient;

public enum Role
{
    User,
    Assistant,
    System
}

/// <summary>One bubble in the transcript. Raises change notifications so the
/// live "thinking…" timer and the final reply update the UI in place.</summary>
public class ChatMessage : INotifyPropertyChanged
{
    public Role Role { get; init; }
    public DateTime Timestamp { get; init; } = DateTime.Now;

    /// <summary>Image attachments shown above the message text: live previews when
    /// sending, or images downloaded from the server when a transcript reloads.</summary>
    public ObservableCollection<ChatImage> Images { get; } = new();

    private string _text = "";
    public string Text
    {
        get => _text;
        set { _text = value; OnPropertyChanged(nameof(Text)); }
    }

    private bool _isBusy;
    public bool IsBusy
    {
        get => _isBusy;
        set { _isBusy = value; OnPropertyChanged(nameof(IsBusy)); }
    }

    private int _elapsed;
    public int Elapsed
    {
        get => _elapsed;
        set { _elapsed = value; OnPropertyChanged(nameof(Elapsed)); OnPropertyChanged(nameof(BusyText)); }
    }

    /// <summary>When &gt; 0 the request is waiting behind other clients on the same
    /// session; the bubble shows the queue position instead of the thinking timer.</summary>
    private int _queuePosition;
    public int QueuePosition
    {
        get => _queuePosition;
        set { _queuePosition = value; OnPropertyChanged(nameof(QueuePosition)); OnPropertyChanged(nameof(BusyText)); }
    }

    public string BusyText => QueuePosition > 0
        ? $"排队中…  前面还有 {QueuePosition} 个请求"
        : $"正在思考…  {Elapsed}s";

    public string Header => Role switch
    {
        Role.User => $"你 · {Timestamp:HH:mm}",
        Role.Assistant => $"Copilot · {Timestamp:HH:mm}",
        _ => $"系统 · {Timestamp:HH:mm}",
    };

    public event PropertyChangedEventHandler? PropertyChanged;
    private void OnPropertyChanged(string name) =>
        PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(name));
}

/// <summary>A displayable image (in a bubble or the pending-attachment strip).
/// <see cref="Source"/> may be assigned asynchronously after a server download,
/// so it raises change notifications.</summary>
public class ChatImage : INotifyPropertyChanged
{
    public string Name { get; init; } = "";
    /// <summary>Set for outgoing/pending images so the send path can upload them.</summary>
    public string? Mime { get; init; }
    public string? DataBase64 { get; init; }

    private ImageSource? _source;
    public ImageSource? Source
    {
        get => _source;
        set { _source = value; OnPropertyChanged(nameof(Source)); }
    }

    public event PropertyChangedEventHandler? PropertyChanged;
    private void OnPropertyChanged(string name) =>
        PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(name));
}

/// <summary>An image the client uploads with a chat turn (base64, no data: prefix).</summary>
public class OutgoingImage
{
    public string Name { get; init; } = "";
    public string Mime { get; init; } = "image/png";
    public string Data { get; init; } = "";
}

// ----- API DTOs -----

public class HealthInfo
{
    [JsonPropertyName("status")] public string Status { get; set; } = "";
    [JsonPropertyName("scope")] public string Scope { get; set; } = "";
    [JsonPropertyName("authMode")] public string AuthMode { get; set; } = "";
}

public class StartJob
{
    [JsonPropertyName("jobId")] public string JobId { get; set; } = "";
    [JsonPropertyName("status")] public string Status { get; set; } = "";
    [JsonPropertyName("error")] public string? Error { get; set; }
    [JsonPropertyName("conversationId")] public string? ConversationId { get; set; }
    [JsonPropertyName("sessionId")] public string? SessionId { get; set; }
    [JsonPropertyName("title")] public string? Title { get; set; }
    [JsonPropertyName("queuePosition")] public int QueuePosition { get; set; }
    [JsonPropertyName("queueLength")] public int QueueLength { get; set; }
}

public class ChatStatus
{
    [JsonPropertyName("status")] public string Status { get; set; } = "";
    [JsonPropertyName("ok")] public bool Ok { get; set; }
    [JsonPropertyName("reply")] public string Reply { get; set; } = "";
    [JsonPropertyName("exitCode")] public int ExitCode { get; set; }
    [JsonPropertyName("timedOut")] public bool TimedOut { get; set; }
    [JsonPropertyName("conversationId")] public string? ConversationId { get; set; }
    [JsonPropertyName("sessionId")] public string? SessionId { get; set; }
    [JsonPropertyName("title")] public string? Title { get; set; }
    [JsonPropertyName("queuePosition")] public int QueuePosition { get; set; }
    [JsonPropertyName("queueLength")] public int QueueLength { get; set; }
}

// ----- Session DTOs (match the server's camelCase /api/sessions shape) -----

public class SessionSummary
{
    [JsonPropertyName("id")] public string Id { get; set; } = "";
    [JsonPropertyName("title")] public string Title { get; set; } = "";
    [JsonPropertyName("createdAt")] public double CreatedAt { get; set; }
    [JsonPropertyName("updatedAt")] public double UpdatedAt { get; set; }
    [JsonPropertyName("messageCount")] public int MessageCount { get; set; }
}

public class SessionDetail : SessionSummary
{
    [JsonPropertyName("messages")] public List<SessionMessage> Messages { get; set; } = new();
}

public class SessionMessage
{
    [JsonPropertyName("role")] public string Role { get; set; } = "";
    [JsonPropertyName("text")] public string Text { get; set; } = "";
    [JsonPropertyName("ts")] public double Ts { get; set; }
    [JsonPropertyName("ok")] public bool? Ok { get; set; }
    [JsonPropertyName("exitCode")] public int? ExitCode { get; set; }
    [JsonPropertyName("attachments")] public List<AttachmentInfo>? Attachments { get; set; }
}

/// <summary>Metadata for a file the client sent with a turn (server-stored).</summary>
public class AttachmentInfo
{
    [JsonPropertyName("name")] public string Name { get; set; } = "";
    [JsonPropertyName("mime")] public string Mime { get; set; } = "";
    [JsonPropertyName("url")] public string Url { get; set; } = "";
    [JsonPropertyName("size")] public long Size { get; set; }
    [JsonPropertyName("kind")] public string Kind { get; set; } = "";
}

// ----- Sidebar view model -----

/// <summary>One row in the session sidebar. Raises change notifications so a
/// title/order change after a reply updates the list in place.</summary>
public class SessionListItem : INotifyPropertyChanged
{
    public SessionListItem(SessionSummary s)
    {
        Id = s.Id;
        _title = s.Title ?? "";
        _updatedAt = s.UpdatedAt;
        MessageCount = s.MessageCount;
    }

    public string Id { get; }

    private string _title;
    public string Title
    {
        get => _title;
        set { _title = value ?? ""; OnPropertyChanged(nameof(Title)); OnPropertyChanged(nameof(DisplayTitle)); }
    }

    public string DisplayTitle => string.IsNullOrWhiteSpace(_title) ? "(未命名)" : _title;

    private double _updatedAt;
    public double UpdatedAt
    {
        get => _updatedAt;
        set { _updatedAt = value; OnPropertyChanged(nameof(UpdatedAt)); OnPropertyChanged(nameof(RelativeTime)); }
    }

    public int MessageCount { get; set; }

    public string RelativeTime => TimeUtil.Relative(_updatedAt);

    private bool _isSelected;
    public bool IsSelected
    {
        get => _isSelected;
        set { if (_isSelected != value) { _isSelected = value; OnPropertyChanged(nameof(IsSelected)); } }
    }

    public void UpdateFrom(SessionSummary s)
    {
        Title = s.Title ?? "";
        UpdatedAt = s.UpdatedAt;
        MessageCount = s.MessageCount;
    }

    /// <summary>Re-evaluate the relative time string against the current wall clock.</summary>
    public void RefreshRelativeTime() => OnPropertyChanged(nameof(RelativeTime));

    public event PropertyChangedEventHandler? PropertyChanged;
    private void OnPropertyChanged(string name) =>
        PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(name));
}

/// <summary>Epoch-seconds &lt;-&gt; local time helpers and relative-time formatting.</summary>
public static class TimeUtil
{
    public static DateTime ToLocalTime(double epochSeconds) =>
        DateTimeOffset.FromUnixTimeMilliseconds((long)Math.Round(epochSeconds * 1000)).LocalDateTime;

    public static double NowEpoch() =>
        DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0;

    public static string Relative(double epochSeconds)
    {
        if (epochSeconds <= 0) return "";
        var span = DateTime.Now - ToLocalTime(epochSeconds);
        if (span < TimeSpan.Zero) span = TimeSpan.Zero;
        if (span.TotalSeconds < 60) return "刚刚";
        if (span.TotalMinutes < 60) return $"{(int)span.TotalMinutes} 分钟前";
        if (span.TotalHours < 24) return $"{(int)span.TotalHours} 小时前";
        if (span.TotalDays < 30) return $"{(int)span.TotalDays} 天前";
        if (span.TotalDays < 365) return $"{(int)(span.TotalDays / 30)} 个月前";
        return $"{(int)(span.TotalDays / 365)} 年前";
    }
}
