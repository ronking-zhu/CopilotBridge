using CopilotBridgeAndroid.Services;

namespace CopilotBridgeAndroid;

public partial class App : Application
{
	/// <summary>Process-wide API client shared by every page.</summary>
	public static BridgeApiClient Api { get; } = new();

	/// <summary>Persisted settings (server url, api key, active session).</summary>
	public static AppSettings Settings { get; private set; } = new();

	private static Task? _loadTask;

	public App()
	{
		InitializeComponent();
		_loadTask = LoadSettingsAsync();
	}

	protected override Window CreateWindow(IActivationState? activationState)
	{
		return new Window(new AppShell());
	}

	/// <summary>Awaitable, idempotent settings load. Pages call this in OnAppearing
	/// before touching <see cref="Api"/> or <see cref="Settings"/> so the
	/// SecureStorage-backed API key is read exactly once.</summary>
	public static Task EnsureSettingsLoadedAsync() => _loadTask ??= LoadSettingsAsync();

	/// <summary>Push the current persisted settings onto the shared API client.
	/// Cheap and safe to call from any page's OnAppearing so the client always
	/// reflects the latest server URL + key (e.g. after the Settings page saved).</summary>
	public static void ApplySettingsToApi()
	{
		Api.BaseUrl = Settings.ServerUrl;
		Api.ApiKey = Settings.ApiKey;
	}

	private static async Task LoadSettingsAsync()
	{
		Settings = await AppSettings.LoadAsync();
		Api.BaseUrl = Settings.ServerUrl;
		Api.ApiKey = Settings.ApiKey;
	}
}