using CopilotBridgeAndroid.Pages;

namespace CopilotBridgeAndroid;

public partial class AppShell : Shell
{
	public AppShell()
	{
		InitializeComponent();

		// Detail pages are reached by push navigation (GoToAsync) from the list.
		Routing.RegisterRoute(nameof(ChatPage), typeof(ChatPage));
		Routing.RegisterRoute(nameof(SettingsPage), typeof(SettingsPage));
	}
}
