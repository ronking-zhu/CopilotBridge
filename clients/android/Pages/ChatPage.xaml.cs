using System.Collections.ObjectModel;
using System.IO;
using System.Linq;
using System.Net;
using CopilotBridgeAndroid.Models;
using CopilotBridgeAndroid.Services;
using Microsoft.Maui.ApplicationModel;
using Microsoft.Maui.Dispatching;
using Microsoft.Maui.Storage;

namespace CopilotBridgeAndroid.Pages;

/// <summary>One conversation. The session id (== conversationId == Copilot
/// --session-id) arrives as the "id" route query parameter from the list.</summary>
[QueryProperty(nameof(SessionId), "id")]
public partial class ChatPage : ContentPage
{
    private BridgeApiClient Api => App.Api;

    public ObservableCollection<ChatMessage> Messages { get; } = new();

    /// <summary>Images picked but not yet sent (the input preview strip).</summary>
    public ObservableCollection<ChatImage> PendingImages { get; } = new();

    // Shell sets this from the navigation query before OnAppearing runs.
    public string SessionId { get; set; } = "";

    private readonly IDispatcherTimer _busyTimer;
    private ChatMessage? _busy;
    private DateTime _busyStart;
    private bool _sending;

    // Cancels in-flight load/send polling when the user leaves the page.
    private CancellationTokenSource _pageCts = new();

    public ChatPage()
    {
        InitializeComponent();
        MessagesView.ItemsSource = Messages;
        BindableLayout.SetItemsSource(PendingStrip, PendingImages);
        PendingImages.CollectionChanged += (_, _) =>
            PendingScroll.IsVisible = PendingImages.Count > 0;

        _busyTimer = Dispatcher.CreateTimer();
        _busyTimer.Interval = TimeSpan.FromSeconds(1);
        _busyTimer.Tick += (_, _) =>
        {
            if (_busy is not null)
            {
                _busy.Elapsed = (int)(DateTime.Now - _busyStart).TotalSeconds;
                ScrollToEnd();
            }
        };
    }

    // ---------- Lifecycle ----------

    protected override async void OnAppearing()
    {
        base.OnAppearing();
        await App.EnsureSettingsLoadedAsync();
        if (_pageCts.IsCancellationRequested) _pageCts = new CancellationTokenSource();
        await LoadSessionAsync();
    }

    protected override void OnDisappearing()
    {
        base.OnDisappearing();
        _busyTimer.Stop();
        try { _pageCts.Cancel(); } catch { /* already disposed/cancelled */ }
    }

    // ---------- Load transcript ----------

    private async Task LoadSessionAsync()
    {
        var ct = _pageCts.Token;

        // Instant render from the local cache.
        var cached = LocalSessionCache.Load(SessionId);
        if (cached is not null) { Title = Display(cached.Title); RenderTranscript(cached.Messages); }
        else { Messages.Clear(); Title = "Copilot Bridge"; }

        // Authoritative refresh from the server.
        try
        {
            var detail = await Api.GetSessionAsync(SessionId, ct);
            if (detail is null) { AddSystem("该会话在服务器端已不存在。"); return; }
            Title = Display(detail.Title);
            RenderTranscript(detail.Messages);
            LocalSessionCache.Save(detail);
        }
        catch (OperationCanceledException) { /* left the page */ }
        catch (ApiException ex) when (ex.StatusCode == HttpStatusCode.Unauthorized)
        {
            AddSystem("401 未授权：请在「设置」里检查 API Key。");
        }
        catch
        {
            if (cached is null) AddSystem("离线：无法加载该会话历史，连接后将自动同步。");
        }
    }

    private void RenderTranscript(IEnumerable<SessionMessage> messages)
    {
        Messages.Clear();
        foreach (var m in messages)
        {
            var role = (m.Role ?? "").ToLowerInvariant() switch
            {
                "user" => Role.User,
                "assistant" => Role.Assistant,
                _ => Role.System,
            };
            var cm = new ChatMessage
            {
                Role = role,
                Text = m.Text ?? "",
                Timestamp = m.Ts > 0 ? TimeUtil.ToLocalTime(m.Ts) : DateTime.Now,
            };
            LoadAttachmentImages(cm, m.Attachments);
            Messages.Add(cm);
        }
        ScrollToEnd();
    }

    // ---------- Send + poll ----------

    private async void OnSendClicked(object? sender, EventArgs e)
    {
        if (_sending) return;

        string text = (InputEntry.Text ?? "").Trim();
        if (string.IsNullOrEmpty(text) && PendingImages.Count == 0) return;
        if (string.IsNullOrEmpty(SessionId)) return;

        // Snapshot the pending images for upload + the optimistic bubble.
        var outImages = PendingImages
            .Where(p => !string.IsNullOrEmpty(p.DataBase64))
            .Select(p => new OutgoingImage { Name = p.Name, Mime = p.Mime ?? "image/png", Data = p.DataBase64! })
            .ToList();
        var userMsg = new ChatMessage { Role = Role.User, Text = text };
        foreach (var p in PendingImages)
            userMsg.Images.Add(new ChatImage { Name = p.Name, Source = p.Source });
        Messages.Add(userMsg);
        InputEntry.Text = "";
        PendingImages.Clear();
        ScrollToEnd();

        var busy = new ChatMessage { Role = Role.Assistant, IsBusy = true, Elapsed = 0 };
        Messages.Add(busy);
        _busy = busy;
        _busyStart = DateTime.Now;
        _busyTimer.Start();
        _sending = true;
        SendBtn.IsEnabled = false;
        ScrollToEnd();

        var ct = _pageCts.Token;
        try
        {
            StartJob job = await Api.StartChatAsync(text, SessionId, false, outImages, ct);
            if (string.IsNullOrEmpty(job.JobId))
                throw new ApiException(job.Error ?? "服务器未返回 jobId");

            // Show an initial queue position (other clients may be ahead on this session).
            busy.QueuePosition = job.Status == "queued" ? job.QueuePosition : 0;

            ChatStatus status;
            while (true)
            {
                await Task.Delay(800, ct);
                status = await Api.GetChatStatusAsync(job.JobId, ct);
                busy.QueuePosition = status.Status == "queued" ? status.QueuePosition : 0;
                if (status.Status == "done") break;
            }

            busy.IsBusy = false;
            if (status.Ok)
                busy.Text = string.IsNullOrWhiteSpace(status.Reply) ? "(无输出)" : status.Reply;
            else
                busy.Text = $"⚠ Copilot 返回错误：{status.Reply}";

            await RefreshAfterReplyAsync(status.Title, ct);
        }
        catch (OperationCanceledException)
        {
            // Left the page mid-reply; the server keeps processing and the reply
            // will be loaded from the cache/server next time this session opens.
        }
        catch (ApiException ex) when (ex.StatusCode == HttpStatusCode.Unauthorized)
        {
            busy.IsBusy = false;
            busy.Text = "鉴权失败：请在「设置」里填写正确的 API Key。";
        }
        catch (Exception ex)
        {
            busy.IsBusy = false;
            busy.Text = $"无法连接服务器：{ex.Message}";
        }
        finally
        {
            _busyTimer.Stop();
            _busy = null;
            _sending = false;
            SendBtn.IsEnabled = true;
            ScrollToEnd();
        }
    }

    /// <summary>After a reply: adopt the server-derived title and refresh the local
    /// transcript cache from the authoritative server copy. Best-effort.</summary>
    private async Task RefreshAfterReplyAsync(string? titleFromChat, CancellationToken ct)
    {
        if (!string.IsNullOrWhiteSpace(titleFromChat)) Title = titleFromChat!;
        try
        {
            var detail = await Api.GetSessionAsync(SessionId, ct);
            if (detail is null) return;
            LocalSessionCache.Save(detail);
            Title = Display(detail.Title);
        }
        catch (OperationCanceledException) { }
        catch { /* best-effort cache refresh */ }
    }

    // ---------- Helpers ----------

    private void AddSystem(string text) => Messages.Add(new ChatMessage { Role = Role.System, Text = text });

    // ---------- Image attachments ----------

    private static string MimeFromName(string name)
    {
        var ext = Path.GetExtension(name).ToLowerInvariant();
        return ext switch
        {
            ".png" => "image/png",
            ".jpg" or ".jpeg" => "image/jpeg",
            ".gif" => "image/gif",
            ".webp" => "image/webp",
            ".bmp" => "image/bmp",
            ".heic" => "image/heic",
            ".heif" => "image/heif",
            _ => "image/png",
        };
    }

    private static ImageSource FromBytes(byte[] data) =>
        ImageSource.FromStream(() => new MemoryStream(data));

    private async void OnAttachClicked(object? sender, EventArgs e)
    {
        try
        {
            var results = await FilePicker.Default.PickMultipleAsync(new PickOptions
            {
                PickerTitle = "添加图片",
                FileTypes = FilePickerFileType.Images,
            });
            if (results is null) return;
            foreach (var file in results)
            {
                try
                {
                    using var stream = await file.OpenReadAsync();
                    using var ms = new MemoryStream();
                    await stream.CopyToAsync(ms);
                    var bytes = ms.ToArray();
                    if (bytes.Length == 0) continue;
                    var name = file.FileName ?? "image";
                    PendingImages.Add(new ChatImage
                    {
                        Name = name,
                        Mime = MimeFromName(name),
                        DataBase64 = Convert.ToBase64String(bytes),
                        Source = FromBytes(bytes),
                    });
                }
                catch { /* skip a file we can't read */ }
            }
        }
        catch (Exception ex)
        {
            AddSystem($"选择图片失败：{ex.Message}");
        }
    }

    private void OnRemovePending(object? sender, EventArgs e)
    {
        if (sender is BindableObject bo && bo.BindingContext is ChatImage img)
            PendingImages.Remove(img);
    }

    /// <summary>Start background downloads of a message's server-stored images and
    /// assign them to the bubble (on the UI thread) as each arrives.</summary>
    private void LoadAttachmentImages(ChatMessage msg, IEnumerable<AttachmentInfo>? attachments)
    {
        if (attachments is null) return;
        foreach (var a in attachments)
        {
            if (a is null || string.IsNullOrEmpty(a.Url)) continue;
            var img = new ChatImage { Name = a.Name };
            msg.Images.Add(img);
            _ = LoadOneAttachmentAsync(img, a.Url);
        }
    }

    private async Task LoadOneAttachmentAsync(ChatImage img, string url)
    {
        try
        {
            var bytes = await Api.GetAttachmentBytesAsync(url, _pageCts.Token);
            MainThread.BeginInvokeOnMainThread(() => img.Source = FromBytes(bytes));
        }
        catch { /* leave blank if the image can't be fetched */ }
    }

    private void ScrollToEnd()
    {
        if (Messages.Count == 0) return;
        try { MessagesView.ScrollTo(Messages.Count - 1, position: ScrollToPosition.End, animate: false); }
        catch { /* view not realized yet */ }
    }

    private static string Display(string? t) => string.IsNullOrWhiteSpace(t) ? "(未命名)" : t!;
}
