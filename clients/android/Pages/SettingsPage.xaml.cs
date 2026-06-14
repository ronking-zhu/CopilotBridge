using System.Net;
using CopilotBridgeAndroid.Services;
using Microsoft.Maui.Graphics;

namespace CopilotBridgeAndroid.Pages;

public partial class SettingsPage : ContentPage
{
    private BridgeApiClient Api => App.Api;

    public SettingsPage()
    {
        InitializeComponent();
    }

    protected override async void OnAppearing()
    {
        base.OnAppearing();
        await App.EnsureSettingsLoadedAsync();
        ServerEntry.Text = App.Settings.ServerUrl;
        KeyEntry.Text = App.Settings.ApiKey;
    }

    protected override async void OnDisappearing()
    {
        base.OnDisappearing();
        await PersistAsync();
    }

    private async Task PersistAsync()
    {
        App.Settings.ServerUrl = (ServerEntry.Text ?? "").Trim();
        App.Settings.ApiKey = KeyEntry.Text ?? "";
        Api.BaseUrl = App.Settings.ServerUrl;
        Api.ApiKey = App.Settings.ApiKey;
        await App.Settings.SaveAsync();
    }

    private async void OnCheckClicked(object? sender, EventArgs e)
    {
        await PersistAsync();
        SetStatus(null, "正在检测连接…");
        CheckBtn.IsEnabled = false;
        try
        {
            var h = await Api.HealthAsync();
            string auth = h.AuthMode == "production" ? "需鉴权" : h.AuthMode;
            SetStatus(true, $"已连接 · scope={h.Scope} · {auth}");
        }
        catch (ApiException ex) when (ex.StatusCode == HttpStatusCode.Unauthorized)
        {
            SetStatus(false, "401 未授权：请检查 API Key。");
        }
        catch (Exception ex)
        {
            SetStatus(false, $"无法连接：{ex.Message}");
        }
        finally
        {
            CheckBtn.IsEnabled = true;
        }
    }

    private void SetStatus(bool? ok, string text)
    {
        StatusDot.Color = ok switch
        {
            true => Color.FromArgb("#2EA043"),
            false => Color.FromArgb("#D13A3A"),
            null => Color.FromArgb("#C99A29"),
        };
        StatusLabel.Text = text;
    }
}
