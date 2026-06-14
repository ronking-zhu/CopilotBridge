using System.Text;
using Microsoft.Maui.ApplicationModel;
using Microsoft.Maui.Controls;
using Microsoft.Maui.Controls.Shapes;
using Microsoft.Maui.Graphics;

namespace CopilotBridgeAndroid.Markdown;

/// <summary>
/// Dependency-free GitHub-Flavored-Markdown -&gt; MAUI views renderer. The MAUI
/// equivalent of the Windows <c>MarkdownRenderer</c>/<c>MarkdownProps</c> pair:
/// instead of a WPF <c>FlowDocument</c> it builds a tree of MAUI controls
/// (<see cref="Label"/>s with <see cref="FormattedString"/>s, <see cref="Grid"/>
/// tables, <see cref="Border"/> code blocks, …) into its own
/// <see cref="VerticalStackLayout"/>. BCL + MAUI only — no NuGet.
///
/// Handles ATX headings, pipe tables, fenced code, lists, blockquotes, horizontal
/// rules and inline bold/italic/code/links. It never throws on malformed input:
/// the whole render is wrapped in try/catch with a raw-text fallback.
/// </summary>
public class MarkdownView : ContentView
{
    // ---- Palette (mirrors the Windows renderer + the assistant bubble theme) ----
    private static readonly Color InlineCodeBg = Color.FromArgb("#ECECEC");
    private static readonly Color CodeBlockBg = Color.FromArgb("#F3F3F3");
    private static readonly Color SoftBorder = Color.FromArgb("#E2E2E2");
    private static readonly Color TableHeaderBg = Color.FromArgb("#F7F7F7");
    private static readonly Color Accent = Color.FromArgb("#0F6CBD");
    private static readonly Color Muted = Color.FromArgb("#5E5E5E");
    private static readonly Color DefaultText = Color.FromArgb("#1F1F1F");

    private const double BaseFontSize = 15;
    private const int MaxInlineDepth = 16;
    // Android's built-in monospace family alias — no font asset to bundle.
    private const string MonoFont = "monospace";

    private readonly VerticalStackLayout _root = new() { Spacing = 4 };

    public MarkdownView()
    {
        Content = _root;
    }

    // ---- Bindable properties ----

    /// <summary>The Markdown source. Changing it re-renders the view tree.</summary>
    public static readonly BindableProperty MarkdownTextProperty =
        BindableProperty.Create(nameof(MarkdownText), typeof(string), typeof(MarkdownView),
            default(string), propertyChanged: OnRenderInputChanged);

    /// <summary>Base text colour (the bubble's text colour). Defaults to #1F1F1F.</summary>
    public static readonly BindableProperty TextColorProperty =
        BindableProperty.Create(nameof(TextColor), typeof(Color), typeof(MarkdownView),
            DefaultText, propertyChanged: OnRenderInputChanged);

    public string? MarkdownText
    {
        get => (string?)GetValue(MarkdownTextProperty);
        set => SetValue(MarkdownTextProperty, value);
    }

    public Color TextColor
    {
        get => (Color)GetValue(TextColorProperty);
        set => SetValue(TextColorProperty, value);
    }

    private static void OnRenderInputChanged(BindableObject bindable, object oldValue, object newValue)
        => ((MarkdownView)bindable).RequestRebuild();

    // Building/clearing the visual tree must happen on the UI thread; data-binding
    // updates usually already arrive there, but guard anyway.
    private void RequestRebuild()
    {
        if (MainThread.IsMainThread) Rebuild();
        else MainThread.BeginInvokeOnMainThread(Rebuild);
    }

    private void Rebuild()
    {
        _root.Clear();
        string source = MarkdownText ?? "";
        try
        {
            foreach (var view in BuildBlocks(source, TextColor ?? DefaultText))
                _root.Add(view);
        }
        catch
        {
            // Defensive: never surface a parser bug to the UI — show the raw text.
            _root.Clear();
            _root.Add(new Label
            {
                Text = source,
                FontSize = BaseFontSize,
                TextColor = TextColor ?? DefaultText,
                LineBreakMode = LineBreakMode.WordWrap,
            });
        }
    }

    // ---------------- Block parsing ----------------

    private List<View> BuildBlocks(string markdown, Color baseColor)
    {
        var views = new List<View>();
        string[] lines = markdown.Replace("\r\n", "\n").Replace("\r", "\n").Split('\n');
        int i = 0;
        ParseBlocks(lines, ref i, lines.Length, views, baseColor);
        return views;
    }

    private void ParseBlocks(string[] lines, ref int i, int end, List<View> outViews, Color baseColor)
    {
        while (i < end)
        {
            string line = lines[i];

            if (string.IsNullOrWhiteSpace(line)) { i++; continue; }

            // Fenced code block (``` or ~~~) — consumed verbatim, no inline parsing.
            if (IsFence(line, out char fenceChar, out int fenceLen))
            {
                i++;
                var code = new StringBuilder();
                bool first = true;
                while (i < end && !IsClosingFence(lines[i], fenceChar, fenceLen))
                {
                    if (!first) code.Append('\n');
                    code.Append(lines[i]);
                    first = false;
                    i++;
                }
                if (i < end) i++; // skip the closing fence
                outViews.Add(BuildCodeBlock(code.ToString(), baseColor));
                continue;
            }

            // ATX heading.
            if (TryHeading(line, out int level, out string headingText))
            {
                outViews.Add(BuildHeading(level, headingText, baseColor));
                i++;
                continue;
            }

            // Horizontal rule.
            if (IsHorizontalRule(line))
            {
                outViews.Add(BuildRule());
                i++;
                continue;
            }

            // Blockquote.
            if (line.TrimStart().StartsWith('>'))
            {
                var inner = new List<string>();
                while (i < end && lines[i].TrimStart().StartsWith('>'))
                {
                    inner.Add(StripQuoteMarker(lines[i]));
                    i++;
                }
                outViews.Add(BuildBlockquote(inner));
                continue;
            }

            // Pipe table: a row of '|' cells followed by a separator row.
            if (i + 1 < end && LooksLikeTableRow(line) && IsTableSeparator(lines[i + 1]))
            {
                outViews.Add(BuildTable(lines, ref i, end, baseColor));
                continue;
            }

            // List (unordered or ordered).
            if (IsListItem(line, out _, out _, out _, out _))
            {
                outViews.Add(BuildList(lines, ref i, end, IndentWidth(line), baseColor));
                continue;
            }

            // Paragraph (default).
            outViews.Add(BuildParagraph(lines, ref i, end, baseColor));
        }
    }

    private bool IsBlockStart(string[] lines, int idx, int end)
    {
        string line = lines[idx];
        if (string.IsNullOrWhiteSpace(line)) return true;
        if (IsFence(line, out _, out _)) return true;
        if (TryHeading(line, out _, out _)) return true;
        if (IsHorizontalRule(line)) return true;
        if (line.TrimStart().StartsWith('>')) return true;
        if (IsListItem(line, out _, out _, out _, out _)) return true;
        if (idx + 1 < end && LooksLikeTableRow(line) && IsTableSeparator(lines[idx + 1])) return true;
        return false;
    }

    // ---------------- Block builders ----------------

    private View BuildParagraph(string[] lines, ref int i, int end, Color baseColor)
    {
        var fs = new FormattedString();
        var style = new InlineStyle { FontSize = BaseFontSize, Color = baseColor };
        bool first = true;
        while (i < end)
        {
            string line = lines[i];
            if (string.IsNullOrWhiteSpace(line)) break;
            if (!first && IsBlockStart(lines, i, end)) break;
            if (!first) fs.Spans.Add(new Span { Text = "\n" });
            foreach (var span in ParseInlines(line.Trim(), style, 0)) fs.Spans.Add(span);
            first = false;
            i++;
        }
        if (fs.Spans.Count == 0) fs.Spans.Add(new Span { Text = "" });
        return new Label
        {
            FormattedText = fs,
            FontSize = BaseFontSize,
            TextColor = baseColor,
            LineBreakMode = LineBreakMode.WordWrap,
            Margin = new Thickness(0, 1, 0, 2),
        };
    }

    private View BuildHeading(int level, string text, Color baseColor)
    {
        double size = level switch
        {
            1 => 22,
            2 => 19.5,
            3 => 17.5,
            4 => 16,
            5 => 15,
            _ => 14,
        };
        var style = new InlineStyle { FontSize = size, Bold = true, Color = baseColor };
        var fs = new FormattedString();
        foreach (var span in ParseInlines(text, style, 0)) fs.Spans.Add(span);
        if (fs.Spans.Count == 0) fs.Spans.Add(new Span { Text = "" });
        return new Label
        {
            FormattedText = fs,
            FontSize = size,
            FontAttributes = FontAttributes.Bold,
            TextColor = baseColor,
            LineBreakMode = LineBreakMode.WordWrap,
            Margin = new Thickness(0, level <= 2 ? 6 : 4, 0, 2),
        };
    }

    private View BuildCodeBlock(string code, Color baseColor)
    {
        var label = new Label
        {
            Text = code,
            FontFamily = MonoFont,
            FontSize = 13.5,
            TextColor = baseColor,
            LineBreakMode = LineBreakMode.NoWrap, // keep long lines intact; scroll instead
        };
        // Long lines scroll horizontally rather than breaking the bubble layout.
        var scroller = new ScrollView
        {
            Orientation = ScrollOrientation.Horizontal,
            Content = label,
        };
        return new Border
        {
            BackgroundColor = CodeBlockBg,
            Stroke = new SolidColorBrush(SoftBorder),
            StrokeThickness = 1,
            StrokeShape = new RoundRectangle { CornerRadius = new CornerRadius(6) },
            Padding = new Thickness(10),
            Margin = new Thickness(0, 4, 0, 4),
            Content = scroller,
        };
    }

    private static View BuildRule() => new BoxView
    {
        HeightRequest = 1,
        Color = SoftBorder,
        Margin = new Thickness(0, 6, 0, 6),
        HorizontalOptions = LayoutOptions.Fill,
    };

    private View BuildBlockquote(List<string> innerLines)
    {
        // Inner content is rendered muted; a left accent bar marks the quote.
        var inner = new List<View>();
        string[] arr = innerLines.ToArray();
        int j = 0;
        ParseBlocks(arr, ref j, arr.Length, inner, Muted);

        var stack = new VerticalStackLayout { Spacing = 4 };
        foreach (var v in inner) stack.Add(v);
        if (inner.Count == 0) stack.Add(new Label { Text = "", TextColor = Muted });

        var grid = new Grid
        {
            ColumnSpacing = 8,
            Margin = new Thickness(0, 4, 0, 4),
        };
        grid.ColumnDefinitions.Add(new ColumnDefinition(GridLength.Auto));
        grid.ColumnDefinitions.Add(new ColumnDefinition(GridLength.Star));

        var bar = new BoxView
        {
            WidthRequest = 3,
            Color = Accent,
            HorizontalOptions = LayoutOptions.Start,
            VerticalOptions = LayoutOptions.Fill,
        };
        Place(grid, bar, 0, 0);
        Place(grid, stack, 1, 0);
        return grid;
    }

    private View BuildList(string[] lines, ref int i, int end, int baseIndent, Color baseColor)
    {
        IsListItem(lines[i], out _, out bool ordered, out int start, out _);
        var container = new VerticalStackLayout { Spacing = 2, Margin = new Thickness(0, 2, 0, 2) };
        int number = Math.Max(1, start);

        while (i < end)
        {
            string line = lines[i];

            if (string.IsNullOrWhiteSpace(line))
            {
                int j = i;
                while (j < end && string.IsNullOrWhiteSpace(lines[j])) j++;
                if (j < end && IsListItem(lines[j], out int ahead, out _, out _, out _) && ahead >= baseIndent)
                {
                    i = j;
                    continue;
                }
                break;
            }

            if (!IsListItem(line, out int indent, out _, out _, out string content)) break;
            if (indent < baseIndent) break;

            // Deeper indentation -> a nested list, indented by a left margin.
            if (indent >= baseIndent + 2)
            {
                var nested = BuildList(lines, ref i, end, indent, baseColor);
                nested.Margin = new Thickness(18, 0, 0, 0);
                container.Add(nested);
                continue;
            }

            i++;
            string marker = ordered ? $"{number}." : "\u2022";
            number++;

            var row = new Grid { ColumnSpacing = 6 };
            row.ColumnDefinitions.Add(new ColumnDefinition(GridLength.Auto));
            row.ColumnDefinitions.Add(new ColumnDefinition(GridLength.Star));

            var markerLabel = new Label
            {
                Text = marker,
                FontSize = BaseFontSize,
                TextColor = baseColor,
                VerticalOptions = LayoutOptions.Start,
            };

            var style = new InlineStyle { FontSize = BaseFontSize, Color = baseColor };
            var fs = new FormattedString();
            foreach (var span in ParseInlines(content.Trim(), style, 0)) fs.Spans.Add(span);
            if (fs.Spans.Count == 0) fs.Spans.Add(new Span { Text = "" });
            var contentLabel = new Label
            {
                FormattedText = fs,
                FontSize = BaseFontSize,
                TextColor = baseColor,
                LineBreakMode = LineBreakMode.WordWrap,
            };

            Place(row, markerLabel, 0, 0);
            Place(row, contentLabel, 1, 0);
            container.Add(row);
        }

        return container;
    }

    private View BuildTable(string[] lines, ref int i, int end, Color baseColor)
    {
        List<string> header = SplitRow(lines[i]); i++;
        List<TextAlignment> aligns = ParseAlignments(lines[i]); i++;
        int cols = Math.Max(header.Count, aligns.Count);
        if (cols == 0) cols = 1;

        var bodyRows = new List<List<string>>();
        while (i < end && !string.IsNullOrWhiteSpace(lines[i]) &&
               LooksLikeTableRow(lines[i]) && !IsTableSeparator(lines[i]))
        {
            bodyRows.Add(SplitRow(lines[i]));
            i++;
        }

        // Equal star columns keep the whole table inside the 320px bubble; cells
        // word-wrap (incl. CJK) so no horizontal scrolling is needed. Thin
        // gridlines come from a 1px gap revealing the grid's border-coloured back.
        var grid = new Grid
        {
            ColumnSpacing = 1,
            RowSpacing = 1,
            BackgroundColor = SoftBorder,
        };
        for (int c = 0; c < cols; c++)
            grid.ColumnDefinitions.Add(new ColumnDefinition(GridLength.Star));
        for (int r = 0; r < bodyRows.Count + 1; r++)
            grid.RowDefinitions.Add(new RowDefinition(GridLength.Auto));

        for (int c = 0; c < cols; c++)
        {
            var cell = BuildTableCell(c < header.Count ? header[c] : "", AlignAt(aligns, c), bold: true, baseColor, TableHeaderBg);
            Place(grid, cell, c, 0);
        }
        for (int r = 0; r < bodyRows.Count; r++)
        {
            List<string> row = bodyRows[r];
            for (int c = 0; c < cols; c++)
            {
                var cell = BuildTableCell(c < row.Count ? row[c] : "", AlignAt(aligns, c), bold: false, baseColor, Colors.White);
                Place(grid, cell, c, r + 1);
            }
        }

        return new Border
        {
            Stroke = new SolidColorBrush(SoftBorder),
            StrokeThickness = 1,
            StrokeShape = new RoundRectangle { CornerRadius = new CornerRadius(8) },
            Margin = new Thickness(0, 4, 0, 4),
            Padding = new Thickness(0),
            BackgroundColor = SoftBorder,
            Content = grid,
        };
    }

    private View BuildTableCell(string text, TextAlignment align, bool bold, Color baseColor, Color background)
    {
        var style = new InlineStyle { FontSize = BaseFontSize - 1, Bold = bold, Color = baseColor };
        var fs = new FormattedString();
        foreach (var span in ParseInlines(text, style, 0)) fs.Spans.Add(span);
        if (fs.Spans.Count == 0) fs.Spans.Add(new Span { Text = "" });
        var label = new Label
        {
            FormattedText = fs,
            FontSize = BaseFontSize - 1,
            TextColor = baseColor,
            HorizontalTextAlignment = align,
            LineBreakMode = LineBreakMode.WordWrap,
            VerticalOptions = LayoutOptions.Center,
            Margin = new Thickness(8, 5), // padding via margin (cell bg fills behind it)
        };
        return new ContentView { BackgroundColor = background, Content = label };
    }

    // ---------------- Block detection helpers (ported from the Windows renderer) ----------------

    private static bool IsFence(string line, out char fenceChar, out int fenceLen)
    {
        fenceChar = '\0';
        fenceLen = 0;
        string t = line.TrimStart();
        if (t.Length < 3) return false;
        char c = t[0];
        if (c != '`' && c != '~') return false;
        int len = 0;
        while (len < t.Length && t[len] == c) len++;
        if (len < 3) return false;
        fenceChar = c;
        fenceLen = len;
        return true;
    }

    private static bool IsClosingFence(string line, char fenceChar, int fenceLen)
    {
        string t = line.Trim();
        if (t.Length < fenceLen) return false;
        foreach (char ch in t)
            if (ch != fenceChar) return false;
        return t.Length >= fenceLen;
    }

    private static bool TryHeading(string line, out int level, out string text)
    {
        level = 0;
        text = "";
        string t = line.TrimStart();
        int h = 0;
        while (h < t.Length && t[h] == '#') h++;
        if (h < 1 || h > 6) return false;
        if (h >= t.Length || (t[h] != ' ' && t[h] != '\t')) return false;

        string body = t[h..].Trim();
        // Strip an optional closing '#' run ("## Title ##") but keep e.g. "C#".
        int e = body.Length;
        while (e > 0 && body[e - 1] == '#') e--;
        if (e < body.Length && (e == 0 || body[e - 1] == ' '))
            body = body[..e].TrimEnd();

        level = h;
        text = body;
        return true;
    }

    private static bool IsHorizontalRule(string line)
    {
        string t = line.Trim();
        if (t.Length < 3) return false;
        char c = t[0];
        if (c != '-' && c != '*' && c != '_') return false;
        int count = 0;
        foreach (char ch in t)
        {
            if (ch == c) count++;
            else if (ch != ' ') return false;
        }
        return count >= 3;
    }

    private static bool LooksLikeTableRow(string line) => line.Contains('|');

    private static bool IsTableSeparator(string line)
    {
        string t = line.Trim();
        if (!t.Contains('-')) return false;
        var cells = SplitRow(t);
        if (cells.Count == 0) return false;
        foreach (var raw in cells)
        {
            string cell = raw.Trim();
            if (cell.Length == 0) return false;
            int k = 0;
            if (cell[k] == ':') k++;
            int dashes = 0;
            while (k < cell.Length && cell[k] == '-') { k++; dashes++; }
            if (k < cell.Length && cell[k] == ':') k++;
            if (dashes < 1 || k != cell.Length) return false;
        }
        return true;
    }

    private static bool IsListItem(string line, out int indent, out bool ordered, out int start, out string content)
    {
        indent = 0;
        ordered = false;
        start = 1;
        content = "";

        int k = 0;
        while (k < line.Length && (line[k] == ' ' || line[k] == '\t'))
        {
            indent += line[k] == '\t' ? 4 : 1;
            k++;
        }
        if (k >= line.Length) return false;

        char c = line[k];
        if ((c == '-' || c == '*' || c == '+') && k + 1 < line.Length && (line[k + 1] == ' ' || line[k + 1] == '\t'))
        {
            ordered = false;
            content = line[(k + 1)..].TrimStart();
            return true;
        }

        int d = k;
        while (d < line.Length && char.IsDigit(line[d])) d++;
        if (d > k && d < line.Length && (line[d] == '.' || line[d] == ')') &&
            d + 1 < line.Length && (line[d + 1] == ' ' || line[d + 1] == '\t'))
        {
            ordered = true;
            int.TryParse(line[k..d], out start);
            if (start < 1) start = 1;
            content = line[(d + 1)..].TrimStart();
            return true;
        }

        return false;
    }

    private static int IndentWidth(string line)
    {
        int w = 0;
        foreach (char ch in line)
        {
            if (ch == ' ') w++;
            else if (ch == '\t') w += 4;
            else break;
        }
        return w;
    }

    private static string StripQuoteMarker(string line)
    {
        string t = line.TrimStart();
        if (t.StartsWith('>')) t = t[1..];
        if (t.StartsWith(' ')) t = t[1..];
        return t;
    }

    private static List<string> SplitRow(string line)
    {
        string s = line.Trim();
        if (s.StartsWith('|')) s = s[1..];
        if (s.EndsWith('|') && !s.EndsWith("\\|")) s = s[..^1];

        var cells = new List<string>();
        var sb = new StringBuilder();
        for (int i = 0; i < s.Length; i++)
        {
            char c = s[i];
            if (c == '\\' && i + 1 < s.Length && s[i + 1] == '|') { sb.Append('|'); i++; continue; }
            if (c == '|') { cells.Add(sb.ToString().Trim()); sb.Clear(); continue; }
            sb.Append(c);
        }
        cells.Add(sb.ToString().Trim());
        return cells;
    }

    private static List<TextAlignment> ParseAlignments(string separator)
    {
        var result = new List<TextAlignment>();
        foreach (var raw in SplitRow(separator))
        {
            string c = raw.Trim();
            bool left = c.StartsWith(':');
            bool right = c.EndsWith(':');
            result.Add(left && right ? TextAlignment.Center
                     : right ? TextAlignment.End
                     : TextAlignment.Start);
        }
        return result;
    }

    private static TextAlignment AlignAt(List<TextAlignment> aligns, int index)
        => index < aligns.Count ? aligns[index] : TextAlignment.Start;

    // ---------------- Inline parsing ----------------

    /// <summary>Style context threaded through inline parsing. A value type so each
    /// recursion gets an independent copy (cheap "clone").</summary>
    private struct InlineStyle
    {
        public double FontSize;
        public bool Bold;
        public bool Italic;
        public Color Color;
    }

    private List<Span> ParseInlines(string text, InlineStyle style, int depth)
    {
        var result = new List<Span>();
        if (string.IsNullOrEmpty(text)) return result;
        if (depth > MaxInlineDepth) { result.Add(MakeSpan(text, style)); return result; }

        var sb = new StringBuilder();
        int i = 0;
        int n = text.Length;

        void Flush()
        {
            if (sb.Length > 0) { result.Add(MakeSpan(sb.ToString(), style)); sb.Clear(); }
        }

        while (i < n)
        {
            char c = text[i];

            // Inline code — highest precedence, contents are literal.
            if (c == '`')
            {
                int run = 0;
                while (i + run < n && text[i + run] == '`') run++;
                int close = FindClosingBacktick(text, i + run, run);
                if (close >= 0)
                {
                    Flush();
                    result.Add(MakeCodeSpan(text.Substring(i + run, close - (i + run)), style));
                    i = close + run;
                    continue;
                }
                sb.Append('`');
                i++;
                continue;
            }

            // Link [text](url).
            if (c == '[')
            {
                var link = TryParseLink(text, i);
                if (link is { } lk)
                {
                    Flush();
                    result.AddRange(BuildLinkSpans(lk.Text, lk.Url, style, depth));
                    i = lk.End;
                    continue;
                }
                sb.Append('[');
                i++;
                continue;
            }

            // Bold / italic.
            if (c == '*' || c == '_')
            {
                int run = 0;
                while (i + run < n && text[i + run] == c) run++;
                int markerLen = run >= 2 ? 2 : 1;
                int close = FindClosingEmphasis(text, i + markerLen, c, markerLen);
                if (close >= i + markerLen)
                {
                    Flush();
                    string inner = text.Substring(i + markerLen, close - (i + markerLen));
                    InlineStyle nested = style;
                    if (markerLen == 2) nested.Bold = true; else nested.Italic = true;
                    result.AddRange(ParseInlines(inner, nested, depth + 1));
                    i = close + markerLen;
                    continue;
                }
                sb.Append(c);
                i++;
                continue;
            }

            sb.Append(c);
            i++;
        }

        Flush();
        return result;
    }

    private Span MakeSpan(string text, InlineStyle style)
    {
        var span = new Span
        {
            Text = text,
            FontSize = style.FontSize,
            TextColor = style.Color,
            FontAttributes = Attr(style.Bold, style.Italic),
        };
        return span;
    }

    private Span MakeCodeSpan(string code, InlineStyle style)
    {
        // CommonMark: drop one leading and trailing space when both are present.
        if (code.Length >= 2 && code[0] == ' ' && code[^1] == ' ' && code.Trim().Length > 0)
            code = code[1..^1];
        return new Span
        {
            Text = code,
            FontFamily = MonoFont,
            FontSize = style.FontSize - 1,
            TextColor = style.Color,
            BackgroundColor = InlineCodeBg,
        };
    }

    private List<Span> BuildLinkSpans(string label, string url, InlineStyle style, int depth)
    {
        InlineStyle linkStyle = style;
        linkStyle.Color = Accent;
        var spans = ParseInlines(label, linkStyle, depth + 1);
        if (spans.Count == 0)
            spans.Add(MakeSpan(string.IsNullOrEmpty(url) ? label : url, linkStyle));
        foreach (var span in spans)
        {
            span.TextDecorations = TextDecorations.Underline;
            AttachLink(span, url);
        }
        return spans;
    }

    private static FontAttributes Attr(bool bold, bool italic) =>
        (bold ? FontAttributes.Bold : FontAttributes.None) |
        (italic ? FontAttributes.Italic : FontAttributes.None);

    private static int FindClosingBacktick(string text, int from, int run)
    {
        int i = from;
        while (i < text.Length)
        {
            if (text[i] == '`')
            {
                int len = 0;
                while (i + len < text.Length && text[i + len] == '`') len++;
                if (len == run) return i;
                i += len;
            }
            else i++;
        }
        return -1;
    }

    private static int FindClosingEmphasis(string text, int from, char marker, int markerLen)
    {
        int i = from;
        while (i < text.Length)
        {
            if (text[i] == marker)
            {
                int len = 0;
                while (i + len < text.Length && text[i + len] == marker) len++;
                if (len >= markerLen) return i + (len - markerLen);
                i += len;
            }
            else i++;
        }
        return -1;
    }

    private static (string Text, string Url, int End)? TryParseLink(string text, int start)
    {
        // text[start] == '['
        int i = start + 1;
        int depth = 1;
        var label = new StringBuilder();
        while (i < text.Length)
        {
            char c = text[i];
            if (c == '\\' && i + 1 < text.Length) { label.Append(text[i + 1]); i += 2; continue; }
            if (c == '[') { depth++; label.Append(c); i++; continue; }
            if (c == ']') { depth--; i++; if (depth == 0) break; label.Append(']'); continue; }
            label.Append(c);
            i++;
        }
        if (depth != 0) return null;
        if (i >= text.Length || text[i] != '(') return null;
        i++;

        int paren = 1;
        var url = new StringBuilder();
        while (i < text.Length)
        {
            char c = text[i];
            if (c == '\\' && i + 1 < text.Length) { url.Append(text[i + 1]); i += 2; continue; }
            if (c == '(') { paren++; url.Append(c); i++; continue; }
            if (c == ')') { paren--; i++; if (paren == 0) break; url.Append(')'); continue; }
            url.Append(c);
            i++;
        }
        if (paren != 0) return null;

        string href = url.ToString().Trim();
        // Strip an optional "title" and angle brackets.
        int sp = href.IndexOf(' ');
        if (sp >= 0) href = href[..sp];
        if (href.StartsWith('<') && href.EndsWith('>') && href.Length >= 2) href = href[1..^1];

        return (label.ToString(), href, i);
    }

    private static void AttachLink(Span span, string url)
    {
        if (!Uri.TryCreate(url, UriKind.Absolute, out Uri? uri) || !IsSafeScheme(uri)) return;
        var tap = new TapGestureRecognizer();
        tap.Tapped += (_, _) => OpenUrl(uri);
        span.GestureRecognizers.Add(tap);
    }

    private static bool IsSafeScheme(Uri uri) =>
        uri.Scheme == Uri.UriSchemeHttp ||
        uri.Scheme == Uri.UriSchemeHttps ||
        uri.Scheme == Uri.UriSchemeMailto;

    private static async void OpenUrl(Uri uri)
    {
        try { await Launcher.Default.OpenAsync(uri); }
        catch { /* opening the browser is best-effort; never crash the chat UI */ }
    }

    private static void Place(Grid grid, View view, int column, int row)
    {
        grid.Add(view);
        Grid.SetColumn(view, column);
        Grid.SetRow(view, row);
    }
}
