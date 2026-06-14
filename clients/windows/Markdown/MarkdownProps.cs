using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;

namespace CopilotBridgeClient.Markdown;

/// <summary>
/// Attached properties that turn a plain <see cref="RichTextBox"/> into a Markdown
/// view. <see cref="RichTextBox.Document"/> is not directly bindable, so binding
/// <see cref="TextProperty"/> (and optionally <see cref="ForegroundProperty"/>)
/// rebuilds the document via <see cref="MarkdownRenderer"/> whenever either changes.
/// </summary>
public static class MarkdownProps
{
    public static readonly DependencyProperty TextProperty =
        DependencyProperty.RegisterAttached(
            "Text", typeof(string), typeof(MarkdownProps),
            new PropertyMetadata(null, OnChanged));

    public static readonly DependencyProperty ForegroundProperty =
        DependencyProperty.RegisterAttached(
            "Foreground", typeof(Brush), typeof(MarkdownProps),
            new PropertyMetadata(null, OnChanged));

    public static string? GetText(DependencyObject o) => (string?)o.GetValue(TextProperty);
    public static void SetText(DependencyObject o, string? value) => o.SetValue(TextProperty, value);

    public static Brush? GetForeground(DependencyObject o) => (Brush?)o.GetValue(ForegroundProperty);
    public static void SetForeground(DependencyObject o, Brush? value) => o.SetValue(ForegroundProperty, value);

    private static void OnChanged(DependencyObject d, DependencyPropertyChangedEventArgs e)
    {
        if (d is not RichTextBox rtb) return;
        string markdown = GetText(rtb) ?? "";
        Brush foreground = GetForeground(rtb) ?? Brushes.Black;
        rtb.Document = MarkdownRenderer.ToFlowDocument(markdown, foreground);
    }
}
