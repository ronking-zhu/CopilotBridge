using System.Collections.ObjectModel;
using System.Net;
using CopilotBridgeAndroid.Models;
using CopilotBridgeAndroid.Services;
using Microsoft.Maui.Graphics;

namespace CopilotBridgeAndroid.Pages;

public partial class SessionsPage : ContentPage
{
    private BridgeApiClient Api => App.Api;

    public ObservableCollection<SessionListItem> Sessions { get; } = new();

    private bool _connected;
    private bool _cacheLoaded;

    public SessionsPage()
    {
        InitializeComponent();
        SessionsView.ItemsSource = Sessions;
    }

    // ---------- Lifecycle ----------

    protected override async void OnAppearing()
    {
        base.OnAppearing();
        await App.EnsureSettingsLoadedAsync();
        // Reflect any settings saved on the Settings page onto the shared client.
        App.ApplySettingsToApi();

        // Local cache first (once), for instant display; then reconcile with the server.
        if (!_cacheLoaded)
        {
            LoadCachedSessions();
            _cacheLoaded = true;
        }
        await RefreshFromServerAsync();
    }

    private void LoadCachedSessions()
    {
        foreach (var d in LocalSessionCache.LoadAll().OrderByDescending(d => d.UpdatedAt))
            Sessions.Add(new SessionListItem(d));
    }

    private async Task RefreshFromServerAsync()
    {
        await CheckHealthAsync();
        if (_connected) await SyncSessionsAsync();
    }

    // ---------- Toolbar / list events ----------

    private async void OnRefreshing(object? sender, EventArgs e)
    {
        await RefreshFromServerAsync();
        Refresher.IsRefreshing = false;
    }

    private async void OnSettingsClicked(object? sender, EventArgs e)
        => await Shell.Current.GoToAsync(nameof(SettingsPage));

    private async void OnNewClicked(object? sender, EventArgs e)
    {
        await App.EnsureSettingsLoadedAsync();
        if (!_connected) await CheckHealthAsync();
        if (!_connected)
        {
            await DisplayAlert("未连接", "无法新建会话。请先在「设置」里检测连接。", "好的");
            return;
        }
        try
        {
            var s = await Api.CreateSessionAsync();
            Sessions.Insert(0, new SessionListItem(s));
            LocalSessionCache.SaveSummary(s);
            await OpenSessionAsync(s.Id);
        }
        catch (ApiException ex) when (ex.StatusCode == HttpStatusCode.Unauthorized)
        {
            SetStatus(false, "401 未授权：请检查 API Key。");
        }
        catch (Exception ex)
        {
            SetStatus(false, $"新建会话失败：{ex.Message}");
        }
    }

    private async void OnSessionSelected(object? sender, SelectionChangedEventArgs e)
    {
        if (e.CurrentSelection.FirstOrDefault() is not SessionListItem item) return;
        SessionsView.SelectedItem = null; // reset so the same row can be reopened
        await OpenSessionAsync(item.Id);
    }

    private async void OnRenameInvoked(object? sender, EventArgs e)
    {
        if (ItemFrom(sender) is not SessionListItem item) return;

        string? input = await DisplayPromptAsync("重命名会话", "输入新的标题：",
            accept: "确定", cancel: "取消", initialValue: item.Title);
        if (input is null) return; // cancelled

        if (!_connected) await CheckHealthAsync();
        if (!_connected) { SetStatus(false, "未连接：无法重命名。"); return; }
        try
        {
            var summary = await Api.RenameSessionAsync(item.Id, input.Trim());
            if (summary is null)
            {
                SetStatus(false, "重命名失败：该会话在服务器端已不存在。");
                RemoveSession(item.Id);
                return;
            }
            item.UpdateFrom(summary);
            LocalSessionCache.SaveSummary(summary);
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

    private async void OnDeleteInvoked(object? sender, EventArgs e)
    {
        if (ItemFrom(sender) is not SessionListItem item) return;

        bool ok = await DisplayAlert("删除会话",
            $"确定删除「{item.DisplayTitle}」？此操作不可撤销。", "删除", "取消");
        if (!ok) return;

        if (!_connected) await CheckHealthAsync();
        if (!_connected) { SetStatus(false, "未连接：无法删除。"); return; }
        try
        {
            await Api.DeleteSessionAsync(item.Id);
            RemoveSession(item.Id);
        }
        catch (ApiException ex) when (ex.StatusCode == HttpStatusCode.Unauthorized)
        {
            SetStatus(false, "401 未授权：请检查 API Key。");
        }
        catch (Exception ex)
        {
            SetStatus(false, $"删除失败：{ex.Message}");
        }
    }

    // ---------- Navigation ----------

    private async Task OpenSessionAsync(string id)
    {
        App.Settings.ActiveSessionId = id;
        _ = App.Settings.SaveAsync(); // best-effort, don't block navigation
        await Shell.Current.GoToAsync($"{nameof(ChatPage)}?id={Uri.EscapeDataString(id)}");
    }

    // ---------- Health / sync ----------

    private async Task CheckHealthAsync()
    {
        SetStatus(null, "正在检测连接…");
        try
        {
            var h = await Api.HealthAsync();
            _connected = true;
            string auth = h.AuthMode == "production" ? "需鉴权" : h.AuthMode;
            SetStatus(true, $"已连接 · scope={h.Scope} · {auth}");
        }
        catch (Exception ex)
        {
            _connected = false;
            SetStatus(false, $"无法连接：{ex.Message}");
        }
    }

    /// <summary>Reconcile the list with GET /api/sessions. Server wins: add new
    /// sessions, update titles/order, and drop any session the server no longer
    /// has (its local cache file is deleted too).</summary>
    private async Task SyncSessionsAsync()
    {
        List<SessionSummary> server;
        try
        {
            server = await Api.ListSessionsAsync();
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

    // ---------- Helpers ----------

    /// <summary>Resolve the swiped row's item from either the CommandParameter or the
    /// SwipeItem's inherited BindingContext (robust across MAUI versions).</summary>
    private static SessionListItem? ItemFrom(object? sender)
        => (sender as SwipeItem)?.CommandParameter as SessionListItem
           ?? (sender as BindableObject)?.BindingContext as SessionListItem;

    private SessionListItem? FindItem(string id) => Sessions.FirstOrDefault(s => s.Id == id);

    private int IndexOfId(string id)
    {
        for (int i = 0; i < Sessions.Count; i++)
            if (Sessions[i].Id == id) return i;
        return -1;
    }

    private void RemoveSession(string id)
    {
        var item = FindItem(id);
        if (item is not null) Sessions.Remove(item);
        LocalSessionCache.Delete(id);
    }

    private void SetStatus(bool? ok, string text)
    {
        StatusDot.Color = ok switch
        {
            true => Color.FromArgb("#2EA043"),
            false => Color.FromArgb("#D13A3A"),
            _ => Color.FromArgb("#C99A29"),
        };
        StatusText.Text = text;
    }
}
