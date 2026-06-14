using System.Windows;

namespace CopilotBridgeClient;

/// <summary>Minimal themed single-line prompt (used for renaming sessions).</summary>
public partial class InputDialog : Window
{
    public string ResultText { get; private set; } = "";

    public InputDialog(string prompt, string initial = "")
    {
        InitializeComponent();
        PromptText.Text = prompt;
        InputText.Text = initial;
        Loaded += (_, _) =>
        {
            InputText.Focus();
            InputText.SelectAll();
        };
    }

    private void Ok_Click(object sender, RoutedEventArgs e)
    {
        ResultText = InputText.Text.Trim();
        DialogResult = true;
    }

    private void Cancel_Click(object sender, RoutedEventArgs e) => DialogResult = false;
}
