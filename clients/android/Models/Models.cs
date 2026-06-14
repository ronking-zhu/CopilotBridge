using System.Collections.ObjectModel;
using System.ComponentModel;
using System.Text.Json.Serialization;
using Microsoft.Maui.Controls;
using Microsoft.Maui.Graphics;

namespace CopilotBridgeAndroid.Models;

public enum Role
{
    User,
    Assistant,
    System
}

/// <summary>One bubble in the transcript. Raises change notifications so the
/// live "thinking…" timer and the final reply update the UI in place. The
/// alignment/colour helpers are computed so the XAML can bind directly without
/// value converters.</summary>
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
        set
        {
            _isBusy = value;
            OnPropertyChanged(nameof(IsBusy));
            OnPropertyChanged(nameof(IsNotBusy));
            OnPropertyChanged(nameof(IsPlainVisible));
            OnPropertyChanged(nameof(IsAssistantVisible));
        }
    }

    public bool IsNotBusy => !_isBusy;

    /// <summary>Assistant replies are Markdown-rendered; user/system text is shown
    /// verbatim. Role is init-only, so these only flip with <see cref="IsBusy"/>.</summary>
    public bool IsAssistant => Role == Role.Assistant;

    /// <summary>Show the plain text Label: a finished, non-assistant bubble.</summary>
    public bool IsPlainVisible => IsNotBusy && Role != Role.Assistant;

    /// <summary>Show the Markdown view: a finished assistant bubble.</summary>
    public bool IsAssistantVisible => IsNotBusy && Role == Role.Assistant;

    private int _elapsed;
    public int Elapsed
    {
        get => _elapsed;
        set { _elapsed = value; OnPropertyChanged(nameof(Elapsed)); OnPropertyChanged(nameof(BusyText)); }
    }

    /// <summary>When &gt; 0 the request is queued behind other clients on the same
    /// session; the bubble shows the queue position instead of the thinking timer.</summary>
    private int _queuePosition;
    public int QueuePosition
    {
        get => _queuePosition;
        set { _queuePosition = value; OnPropertyChanged(nameof(QueuePosition)); OnPropertyChanged(nameof(BusyText)); }
    }

    public string BusyText => _queuePosition > 0
        ? $"排队中…  前面还有 {_queuePosition} 个请求"
        : $"正在思考…  {Elapsed}s";

    public string Header => Role switch
    {
        Role.User => $"你 · {Timestamp:HH:mm}",
        Role.Assistant => $"Copilot · {Timestamp:HH:mm}",
        _ => $"系统 · {Timestamp:HH:mm}",
    };

    public LayoutOptions BubbleAlignment => Role switch
    {
        Role.User => LayoutOptions.End,
        Role.System => LayoutOptions.Center,
        _ => LayoutOptions.Start,
    };

    public Color BubbleColor => Role switch
    {
        Role.User => Color.FromArgb("#0F6CBD"),
        Role.System => Color.FromArgb("#E6E6E6"),
        _ => Colors.White,
    };

    public Color TextColor => Role switch
    {
        Role.User => Colors.White,
        Role.System => Color.FromArgb("#555555"),
        _ => Color.FromArgb("#1F1F1F"),
    };

    public Color HeaderColor => Role == Role.User ? Color.FromArgb("#DCEBFB") : Color.FromArgb("#8A8A8A");

    public event PropertyChangedEventHandler? PropertyChanged;
    private void OnPropertyChanged(string name) =>
        PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(name));
}

/// <summary>A displayable image (bubble or pending-attachment strip). <see cref="Source"/>
/// may be assigned asynchronously after a server download, so it notifies.</summary>
public class ChatImage : INotifyPropertyChanged
{
    public string Name { get; init; } = "";
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

// ----- API DTOs (match the server's camelCase JSON shapes) -----

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

// ----- Session DTOs (match the server's /api/sessions shape) -----

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

// ----- Session list view model -----

/// <summary>One row in the sessions list. Raises change notifications so a
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

/// <summary>Epoch-seconds &lt;-&gt; local time helpers and relative-time formatting.
/// Server timestamps are epoch SECONDS (double).</summary>
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
