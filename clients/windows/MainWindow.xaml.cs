using System.Collections.ObjectModel;
using System.IO;
using System.Linq;
using System.Net;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using System.Windows.Threading;

namespace CopilotBridgeClient;

public partial class MainWindow : Window
{
    public ObservableCollection<ChatMessage> Messages { get; } = new();
    public ObservableCollection<SessionListItem> Sessions { get; } = new();

    /// <summary>Images picked but not yet sent (the input preview strip).</summary>
    public ObservableCollection<ChatImage> PendingImages { get; } = new();

    private readonly BridgeApiClient _api = new();
    private readonly AppSettings _settings;
    private readonly DispatcherTimer _busyTimer;

    private ChatMessage? _busyMessage;
    private DateTime _busyStart;
    private bool _sending;
    private bool _connected;
    private string? _activeSessionId;
    private bool _suppressSelection;

    public MainWindow()
    {
        InitializeComponent();
        DataContext = this;

        _settings = AppSettings.Load();
        _activeSessionId = string.IsNullOrWhiteSpace(_settings.ActiveSessionId) ? null : _settings.ActiveSessionId;

        ServerBox.Text = _settings.ServerUrl;
        KeyBox.Password = _settings.ApiKey;

        _busyTimer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(1) };
        _busyTimer.Tick += BusyTimer_Tick;

        Messages.CollectionChanged += (_, _) => ScrollToEnd();

        Loaded += async (_, _) => await InitializeAsync();
    }

    // ---------- Startup ----------

    private async Task InitializeAsync()
    {
        // 1. Local cache first, for instant display.
        LoadCachedSessions();
        // 2. Health check, then server sync (server is authoritative for the list).
        await CheckHealthAsync();
        if (_connected) await SyncSessionsAsync();
        // 3. Open the last-active session, the newest one, or an empty placeholder.
        await OpenInitialSessionAsync();
    }

    private void LoadCachedSessions()
    {
        foreach (var d in LocalSessionCache.LoadAll().OrderByDescending(d => d.UpdatedAt))
            Sessions.Add(new SessionListItem(d));
    }

    private async Task OpenInitialSessionAsync()
    {
        SessionListItem? target = null;
        if (!string.IsNullOrEmpty(_activeSessionId)) target = FindItem(_activeSessionId!);
        target ??= Sessions.FirstOrDefault();

        if (target is not null)
        {
            SelectInList(target);
            await OpenSessionAsync(target.Id);
        }
        else
        {
            _activeSessionId = null;
            Messages.Clear();
            ChatTitle.Text = "Copilot Bridge";
            AddSystem("还没有会话。点击「＋ 新会话」或直接在下方输入即可开始。");
        }
    }

    // ---------- UI events ----------

    private async void Connect_Click(object sender, RoutedEventArgs e)
    {
        await CheckHealthAsync();
        if (!_connected) return;
        await SyncSessionsAsync();
        if (!string.IsNullOrEmpty(_activeSessionId) && FindItem(_activeSessionId!) is not null)
            await OpenSessionAsync(_activeSessionId!);
        else
            await OpenInitialSessionAsync();
    }

    private async void NewConversation_Click(object sender, RoutedEventArgs e)
    {
        SyncSettingsFromUi();
        await CreateAndSelectNewSessionAsync();
    }

    private async void SessionsList_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (_suppressSelection) return;
        if (SessionsList.SelectedItem is not SessionListItem item) return;
        if (item.Id == _activeSessionId) return;
        await OpenSessionAsync(item.Id);
    }

    private async void RenameSession_Click(object sender, RoutedEventArgs e)
    {
        var item = ItemFromMenu(sender);
        if (item is null) return;

        var dlg = new InputDialog("重命名会话：", item.Title) { Owner = this };
        if (dlg.ShowDialog() != true) return;

        SyncSettingsFromUi();
        if (!_connected) { SetStatus(false, "未连接：无法重命名。"); return; }
        try
        {
            var summary = await _api.RenameSessionAsync(item.Id, dlg.ResultText);
            if (summary is null)
            {
                SetStatus(false, "重命名失败：该会话在服务器端已不存在。");
                await DropMissingSessionAsync(item.Id);
                return;
            }
            item.UpdateFrom(summary);
            LocalSessionCache.SaveSummary(summary);
            if (_activeSessionId == item.Id) ChatTitle.Text = item.DisplayTitle;
        }
        catch (ApiException ex) when (ex.StatusCode == HttpStatusCode.Unauthorized)
        {
            SetStatus(false, "401 未授权：请检查 API Key。");
        }
        catch (Exception ex)
        {
            SetStatus(false, $"重命名失败：{ex.Message}");
        }
    }

    private async void DeleteSession_Click(object sender, RoutedEventArgs e)
    {
        var item = ItemFromMenu(sender);
        if (item is null) return;

        var confirm = MessageBox.Show(this,
            $"确定删除会话「{item.DisplayTitle}」？此操作不可撤销。",
            "删除会话", MessageBoxButton.OKCancel, MessageBoxImage.Warning);
        if (confirm != MessageBoxResult.OK) return;

        SyncSettingsFromUi();
        if (!_connected) { SetStatus(false, "未连接：无法删除。"); return; }
        try
        {
            await _api.DeleteSessionAsync(item.Id);
        }
        catch (ApiException ex) when (ex.StatusCode == HttpStatusCode.Unauthorized)
        {
            SetStatus(false, "401 未授权：请检查 API Key。");
            return;
        }
        catch (Exception ex)
        {
            SetStatus(false, $"删除失败：{ex.Message}");
            return;
        }

        bool wasActive = _activeSessionId == item.Id;
        int idx = Sessions.IndexOf(item);
        RemoveSession(item.Id);

        if (!wasActive) return;

        _activeSessionId = null;
        SaveSettings();
        var next = Sessions.ElementAtOrDefault(Math.Min(idx, Sessions.Count - 1));
        if (next is not null)
        {
            SelectInList(next);
            await OpenSessionAsync(next.Id);
        }
        else
        {
            Messages.Clear();
            ChatTitle.Text = "Copilot Bridge";
            AddSystem("已删除最后一个会话。点击「＋ 新会话」或直接输入即可开始。");
        }
    }

    private void InputBox_PreviewKeyDown(object sender, KeyEventArgs e)
    {
        if (e.Key == Key.Enter && (Keyboard.Modifiers & ModifierKeys.Shift) == 0)
        {
            e.Handled = true;
            _ = SendAsync();
        }
    }

    private async void Send_Click(object sender, RoutedEventArgs e) => await SendAsync();

    // ---------- Image attachments ----------

    private static ImageSource BitmapFromBytes(byte[] data)
    {
        using var ms = new MemoryStream(data);
        var bmp = new BitmapImage();
        bmp.BeginInit();
        bmp.CacheOption = BitmapCacheOption.OnLoad;
        bmp.StreamSource = ms;
        bmp.EndInit();
        bmp.Freeze();
        return bmp;
    }

    private static string MimeFromName(string name)
    {
        var ext = System.IO.Path.GetExtension(name).ToLowerInvariant();
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

    private void Attach_Click(object sender, RoutedEventArgs e)
    {
        var dlg = new Microsoft.Win32.OpenFileDialog
        {
            Title = "添加图片",
            Multiselect = true,
            Filter = "图片|*.png;*.jpg;*.jpeg;*.gif;*.webp;*.bmp;*.heic;*.heif|所有文件|*.*",
        };
        if (dlg.ShowDialog(this) != true) return;
        foreach (var path in dlg.FileNames)
        {
            try
            {
                byte[] bytes = File.ReadAllBytes(path);
                string name = System.IO.Path.GetFileName(path);
                PendingImages.Add(new ChatImage
                {
                    Name = name,
                    Mime = MimeFromName(name),
                    DataBase64 = Convert.ToBase64String(bytes),
                    Source = BitmapFromBytes(bytes),
                });
            }
            catch (Exception ex)
            {
                SetStatus(false, $"无法读取图片：{ex.Message}");
            }
        }
    }

    private void RemovePending_Click(object sender, RoutedEventArgs e)
    {
        if (sender is FrameworkElement fe && fe.Tag is ChatImage img)
            PendingImages.Remove(img);
    }

    /// <summary>Start background downloads of a message's server-stored images and
    /// assign them to the bubble as each arrives.</summary>
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
            byte[] bytes = await _api.GetAttachmentBytesAsync(url);
            img.Source = BitmapFromBytes(bytes);
        }
        catch { /* leave blank if the image can't be fetched */ }
    }

    /// <summary>The Markdown <see cref="RichTextBox"/> swallows the wheel event; re-raise
    /// it on the transcript <c>ScrollViewer</c> so scrolling still works over a reply.</summary>
    private void AssistantRtb_PreviewMouseWheel(object sender, MouseWheelEventArgs e)
    {
        if (e.Handled) return;
        e.Handled = true;
        var forwarded = new MouseWheelEventArgs(e.MouseDevice, e.Timestamp, e.Delta)
        {
            RoutedEvent = UIElement.MouseWheelEvent,
            Source = sender,
        };
        Scroller.RaiseEvent(forwarded);
    }

    // ---------- Health / sync ----------

    private async Task CheckHealthAsync()
    {
        SyncSettingsFromUi();
        SetStatus(null, "正在检测连接…");
        try
        {
            var h = await _api.HealthAsync();
            _connected = true;
            string auth = h.AuthMode == "production" ? "需鉴权" : h.AuthMode;
            SetStatus(true, $"已连接 · scope={h.Scope} · {auth}");
        }
        catch (Exception ex)
        {
            _connected = false;
            SetStatus(false, $"无法连接：{ex.Message}");
            SettingsExpander.IsExpanded = true;
        }
    }

    /// <summary>Reconcile the sidebar with GET /api/sessions. Server wins for the
    /// list: add new sessions, update titles/order, and drop any session the server
    /// no longer has (its local cache file is deleted too).</summary>
    private async Task SyncSessionsAsync()
    {
        List<SessionSummary> server;
        try
        {
            server = await _api.ListSessionsAsync();
        }
        catch (ApiException ex) when (ex.StatusCode == HttpStatusCode.Unauthorized)
        {
            SetStatus(false, "401 未授权：请检查 API Key。");
            return;
        }
        catch (Exception ex)
        {
            SetStatus(false, $"无法获取会话列表：{ex.Message}");
            return;
        }

        var serverIds = new HashSet<string>(server.Select(s => s.Id));

        _suppressSelection = true;
        try
        {
            // Drop local-only sessions (server no longer has them).
            for (int i = Sessions.Count - 1; i >= 0; i--)
            {
                if (!serverIds.Contains(Sessions[i].Id))
                {
                    LocalSessionCache.Delete(Sessions[i].Id);
                    Sessions.RemoveAt(i);
                }
            }
            // Upsert every server summary.
            foreach (var s in server)
            {
                var existing = FindItem(s.Id);
                if (existing is null) Sessions.Add(new SessionListItem(s));
                else existing.UpdateFrom(s);
                LocalSessionCache.SaveSummary(s);
            }
            // Reorder to match the server order (updatedAt DESC).
            for (int idx = 0; idx < server.Count; idx++)
            {
                if (idx < Sessions.Count && Sessions[idx].Id == server[idx].Id) continue;
                int cur = IndexOfId(server[idx].Id);
                if (cur >= 0 && cur != idx) Sessions.Move(cur, idx);
            }
        }
        finally
        {
            _suppressSelection = false;
        }

        // If the active session vanished server-side, clear the chat pane.
        if (_activeSessionId is not null && !serverIds.Contains(_activeSessionId))
        {
            _activeSessionId = null;
            SaveSettings();
            Messages.Clear();
            ChatTitle.Text = "Copilot Bridge";
            AddSystem("当前会话已在服务器端被移除。请从左侧选择或新建会话。");
        }
    }

    // ---------- Session open / transcript ----------

    private async Task OpenSessionAsync(string id)
    {
        _activeSessionId = id;
        SaveSettings();
        ChatTitle.Text = FindItem(id)?.DisplayTitle ?? "Copilot Bridge";

        // Instant render from the local cache.
        var cached = LocalSessionCache.Load(id);
        if (cached is not null) RenderTranscript(cached.Messages);
        else Messages.Clear();

        if (!_connected)
        {
            if (cached is null) AddSystem("离线：无法加载该会话历史，连接后将自动同步。");
            return;
        }

        // Authoritative refresh from the server.
        try
        {
            var detail = await _api.GetSessionAsync(id);
            if (_activeSessionId != id) return; // user switched away mid-fetch

            if (detail is null)
            {
                await DropMissingSessionAsync(id);
                return;
            }

            RenderTranscript(detail.Messages);
            LocalSessionCache.Save(detail);
            var item = FindItem(id);
            if (item is not null)
            {
                item.UpdateFrom(detail);
                ChatTitle.Text = item.DisplayTitle;
            }
        }
        catch (ApiException ex) when (ex.StatusCode == HttpStatusCode.Unauthorized)
        {
            SetStatus(false, "401 未授权：请检查 API Key。");
        }
        catch (Exception ex)
        {
            SetStatus(false, $"加载会话失败：{ex.Message}");
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
    }

    private async Task<bool> CreateAndSelectNewSessionAsync()
    {
        if (!_connected)
        {
            SetStatus(false, "未连接：无法新建会话，请先「检测连接」。");
            SettingsExpander.IsExpanded = true;
            return false;
        }
        try
        {
            var s = await _api.CreateSessionAsync();
            var item = new SessionListItem(s);
            Sessions.Insert(0, item);
            LocalSessionCache.SaveSummary(s);

            _activeSessionId = s.Id;
            SaveSettings();
            SelectInList(item);
            Messages.Clear();
            ChatTitle.Text = item.DisplayTitle;
            AddSystem("已新建会话。直接在下方输入开始对话。");
            InputBox.Focus();
            return true;
        }
        catch (ApiException ex) when (ex.StatusCode == HttpStatusCode.Unauthorized)
        {
            SetStatus(false, "401 未授权：请检查 API Key。");
            return false;
        }
        catch (Exception ex)
        {
            SetStatus(false, $"新建会话失败：{ex.Message}");
            return false;
        }
    }

    // ---------- Send ----------

    private async Task SendAsync()
    {
        if (_sending) { SetStatus(null, "正在等待上一条回复…"); return; }

        string text = InputBox.Text.Trim();
        if (string.IsNullOrEmpty(text) && PendingImages.Count == 0) return;

        SyncSettingsFromUi();
        if (string.IsNullOrWhiteSpace(_api.BaseUrl))
        {
            SetStatus(false, "请先填写服务器地址。");
            SettingsExpander.IsExpanded = true;
            return;
        }

        // Lazily create a session if none is active (server derives the title).
        if (string.IsNullOrEmpty(_activeSessionId))
        {
            if (!await CreateAndSelectNewSessionAsync()) return;
        }
        string sendingId = _activeSessionId!;

        // Snapshot the pending images, build the upload payload + the optimistic bubble.
        var outImages = PendingImages
            .Where(p => !string.IsNullOrEmpty(p.DataBase64))
            .Select(p => new OutgoingImage { Name = p.Name, Mime = p.Mime ?? "image/png", Data = p.DataBase64! })
            .ToList();

        var userMsg = new ChatMessage { Role = Role.User, Text = text };
        foreach (var p in PendingImages)
            userMsg.Images.Add(new ChatImage { Name = p.Name, Source = p.Source });
        Messages.Add(userMsg);
        InputBox.Clear();
        PendingImages.Clear();

        var busy = new ChatMessage { Role = Role.Assistant, IsBusy = true, Elapsed = 0 };
        Messages.Add(busy);
        _busyMessage = busy;
        _busyStart = DateTime.Now;
        _busyTimer.Start();

        _sending = true;
        SendBtn.IsEnabled = false;

        try
        {
            StartJob job = await _api.StartChatAsync(text, sendingId, false, outImages);
            if (string.IsNullOrEmpty(job.JobId))
                throw new ApiException(job.Error ?? "服务器未返回 jobId");

            // Show an initial queue position (other clients may be ahead on this session).
            busy.QueuePosition = job.Status == "queued" ? job.QueuePosition : 0;

            ChatStatus status;
            while (true)
            {
                await Task.Delay(800);
                status = await _api.GetChatStatusAsync(job.JobId);
                busy.QueuePosition = status.Status == "queued" ? status.QueuePosition : 0;
                if (status.Status == "done") break;
            }

            // Adopt the server's session id defensively (in case it differs).
            string effectiveId = !string.IsNullOrEmpty(status.SessionId) ? status.SessionId!
                               : !string.IsNullOrEmpty(job.SessionId) ? job.SessionId!
                               : sendingId;
            if (effectiveId != sendingId && _activeSessionId == sendingId)
            {
                _activeSessionId = effectiveId;
                SaveSettings();
            }

            bool stillHere = _activeSessionId == sendingId || _activeSessionId == effectiveId;

            busy.IsBusy = false;
            if (status.Ok)
            {
                if (stillHere) busy.Text = string.IsNullOrWhiteSpace(status.Reply) ? "(无输出)" : status.Reply;
                SetStatus(true, $"完成 · exit {status.ExitCode}{(status.TimedOut ? " · 超时" : "")}");
            }
            else
            {
                if (stillHere) busy.Text = $"⚠ Copilot 返回错误：{status.Reply}";
                SetStatus(false, $"Copilot 出错 · exit {status.ExitCode}");
            }

            await RefreshSessionAfterReplyAsync(effectiveId, status.Title, busy);
        }
        catch (ApiException ex) when (ex.StatusCode == HttpStatusCode.Unauthorized)
        {
            FailBusy(busy, "鉴权失败：API Key 不正确或缺失。请在「连接设置」里填写正确的 X-API-Key。");
            SetStatus(false, "401 未授权");
        }
        catch (Exception ex)
        {
            FailBusy(busy, $"无法连接到 app server：{ex.Message}");
            SetStatus(false, "请求失败");
        }
        finally
        {
            _busyTimer.Stop();
            _busyMessage = null;
            _sending = false;
            SendBtn.IsEnabled = true;
            SaveSettings();
            InputBox.Focus();
        }
    }

    /// <summary>After a reply: bump the session to the top, adopt the server-derived
    /// title, and refresh the local transcript cache from the authoritative server
    /// copy. Best-effort — never throws into the send path.</summary>
    private async Task RefreshSessionAfterReplyAsync(string id, string? titleFromChat, ChatMessage busyBubble)
    {
        var item = FindItem(id);
        if (item is null)
        {
            item = new SessionListItem(new SessionSummary
            {
                Id = id,
                Title = titleFromChat ?? "",
                UpdatedAt = TimeUtil.NowEpoch(),
            });
            Sessions.Insert(0, item);
        }
        else
        {
            if (!string.IsNullOrWhiteSpace(titleFromChat)) item.Title = titleFromChat!;
            item.UpdatedAt = TimeUtil.NowEpoch();
            MoveToTop(item);
        }
        if (_activeSessionId == id) ChatTitle.Text = item.DisplayTitle;

        if (!_connected) return;
        try
        {
            var detail = await _api.GetSessionAsync(id);
            if (detail is null) return;
            LocalSessionCache.Save(detail);
            var it = FindItem(id);
            if (it is not null)
            {
                it.UpdateFrom(detail);
                MoveToTop(it);
                if (_activeSessionId == id) ChatTitle.Text = it.DisplayTitle;
            }
            // If this session is active but the inline busy bubble was discarded
            // (user navigated away and back mid-reply), re-render so the reply shows.
            if (_activeSessionId == id && !Messages.Contains(busyBubble))
                RenderTranscript(detail.Messages);
        }
        catch { /* best-effort cache refresh */ }
    }

    private void BusyTimer_Tick(object? sender, EventArgs e)
    {
        if (_busyMessage is not null)
        {
            _busyMessage.Elapsed = (int)(DateTime.Now - _busyStart).TotalSeconds;
            ScrollToEnd();
        }
    }

    // ---------- Sidebar helpers ----------

    private SessionListItem? FindItem(string id) => Sessions.FirstOrDefault(s => s.Id == id);

    private int IndexOfId(string id)
    {
        for (int i = 0; i < Sessions.Count; i++)
            if (Sessions[i].Id == id) return i;
        return -1;
    }

    private void MoveToTop(SessionListItem item)
    {
        int idx = Sessions.IndexOf(item);
        if (idx <= 0) return;
        _suppressSelection = true;
        Sessions.Move(idx, 0);
        _suppressSelection = false;
    }

    private void RemoveSession(string id)
    {
        var item = FindItem(id);
        if (item is not null)
        {
            _suppressSelection = true;
            Sessions.Remove(item);
            _suppressSelection = false;
        }
        LocalSessionCache.Delete(id);
    }

    /// <summary>The server reports this id is gone (404): remove it everywhere and,
    /// if it was active, clear the chat pane.</summary>
    private Task DropMissingSessionAsync(string id)
    {
        bool wasActive = _activeSessionId == id;
        RemoveSession(id);
        if (wasActive)
        {
            _activeSessionId = null;
            SaveSettings();
            Messages.Clear();
            ChatTitle.Text = "Copilot Bridge";
            AddSystem("该会话在服务器端已不存在，已从列表移除。");
        }
        return Task.CompletedTask;
    }

    private void SelectInList(SessionListItem? item)
    {
        _suppressSelection = true;
        SessionsList.SelectedItem = item;
        if (item is not null) SessionsList.ScrollIntoView(item);
        _suppressSelection = false;
    }

    private static SessionListItem? ItemFromMenu(object sender)
    {
        if (sender is MenuItem mi)
        {
            if (mi.DataContext is SessionListItem s) return s;
            if (mi.Parent is ContextMenu cm && cm.PlacementTarget is FrameworkElement fe &&
                fe.DataContext is SessionListItem s2) return s2;
        }
        return null;
    }

    // ---------- General helpers ----------

    private void FailBusy(ChatMessage busy, string message)
    {
        busy.IsBusy = false;
        busy.Text = message;
    }

    private void AddSystem(string text) => Messages.Add(new ChatMessage { Role = Role.System, Text = text });

    private void SyncSettingsFromUi()
    {
        _settings.ServerUrl = (ServerBox.Text ?? "").Trim();
        _settings.ApiKey = KeyBox.Password;
        _api.BaseUrl = _settings.ServerUrl;
        _api.ApiKey = _settings.ApiKey;
    }

    private void SaveSettings()
    {
        SyncSettingsFromUi();
        _settings.ActiveSessionId = _activeSessionId ?? "";
        _settings.Save();
    }

    private void SetStatus(bool? ok, string text)
    {
        StatusDot.Fill = ok switch
        {
            true => new SolidColorBrush(Color.FromRgb(0x2E, 0xA0, 0x43)),
            false => new SolidColorBrush(Color.FromRgb(0xD1, 0x3A, 0x3A)),
            null => new SolidColorBrush(Color.FromRgb(0xC9, 0x9A, 0x29)),
        };
        StatusText.Text = text;
    }

    private void ScrollToEnd() => Dispatcher.InvokeAsync(() => Scroller.ScrollToEnd(), DispatcherPriority.Background);

    protected override void OnClosing(System.ComponentModel.CancelEventArgs e)
    {
        SaveSettings();
        base.OnClosing(e);
    }
}
