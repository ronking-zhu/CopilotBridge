using System.Diagnostics;
using System.Text;
using System.Windows;
using System.Windows.Documents;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Navigation;
using DocList = System.Windows.Documents.List;

namespace CopilotBridgeClient.Markdown;

/// <summary>
/// Dependency-free GitHub-Flavored-Markdown -&gt; WPF <see cref="FlowDocument"/> renderer.
/// Handles ATX headings, pipe tables, fenced code, lists, blockquotes, horizontal
/// rules and inline bold/italic/code/links. Built with BCL + WPF only — no NuGet.
/// It never throws on malformed input: anything it cannot parse falls back to text.
/// </summary>
public static class MarkdownRenderer
{
    private static readonly FontFamily BodyFont = new("Segoe UI Variable Text, Segoe UI");
    private static readonly FontFamily MonoFont = new("Cascadia Mono, Consolas, Courier New");

    private static readonly Brush CodeBlockBg = Frozen(0xF3, 0xF3, 0xF3);
    private static readonly Brush InlineCodeBg = Frozen(0xEC, 0xEC, 0xEC);
    private static readonly Brush SoftBorder = Frozen(0xE2, 0xE2, 0xE2);
    private static readonly Brush TableHeaderBg = Frozen(0xF7, 0xF7, 0xF7);
    private static readonly Brush AccentBrush = Frozen(0x0F, 0x6C, 0xBD);
    private static readonly Brush MutedFg = Frozen(0x5E, 0x5E, 0x5E);

    private const double BaseFontSize = 14;
    private const int MaxInlineDepth = 16;

    /// <summary>Render <paramref name="markdown"/> into a fresh <see cref="FlowDocument"/>.
    /// <paramref name="foreground"/> is the bubble's text colour so the body matches.</summary>
    public static FlowDocument ToFlowDocument(string? markdown, Brush foreground)
    {
        var doc = new FlowDocument
        {
            PagePadding = new Thickness(0),
            Background = Brushes.Transparent,
            FontFamily = BodyFont,
            FontSize = BaseFontSize,
            Foreground = foreground,
            TextAlignment = TextAlignment.Left,
        };

        try
        {
            string[] lines = (markdown ?? "")
                .Replace("\r\n", "\n").Replace("\r", "\n")
                .Split('\n');
            int i = 0;
            ParseBlocks(lines, ref i, lines.Length, doc.Blocks, foreground);
        }
        catch
        {
            // Defensive: never surface a parser bug to the UI — show the raw text.
            doc.Blocks.Clear();
            doc.Blocks.Add(new Paragraph(new Run(markdown ?? "")) { Margin = new Thickness(0) });
        }

        if (doc.Blocks.Count == 0)
            doc.Blocks.Add(new Paragraph { Margin = new Thickness(0) });

        return doc;
    }

    // ---------------- Block parsing ----------------

    private static void ParseBlocks(string[] lines, ref int i, int end, BlockCollection blocks, Brush fg)
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
                blocks.Add(BuildCodeBlock(code.ToString()));
                continue;
            }

            // ATX heading.
            if (TryHeading(line, out int level, out string headingText))
            {
                blocks.Add(BuildHeading(level, headingText, fg));
                i++;
                continue;
            }

            // Horizontal rule.
            if (IsHorizontalRule(line))
            {
                blocks.Add(BuildHorizontalRule());
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
                blocks.Add(BuildBlockquote(inner, fg));
                continue;
            }

            // Pipe table: a row of '|' cells followed by a separator row.
            if (i + 1 < end && LooksLikeTableRow(line) && IsTableSeparator(lines[i + 1]))
            {
                blocks.Add(BuildTable(lines, ref i, end, fg));
                continue;
            }

            // List (unordered or ordered).
            if (IsListItem(line, out _, out _, out _, out _))
            {
                blocks.Add(BuildList(lines, ref i, end, IndentWidth(line), fg));
                continue;
            }

            // Paragraph (default).
            blocks.Add(BuildParagraph(lines, ref i, end, fg));
        }
    }

    private static bool IsBlockStart(string[] lines, int idx, int end)
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

    private static Block BuildParagraph(string[] lines, ref int i, int end, Brush fg)
    {
        var p = new Paragraph { Margin = new Thickness(0, 2, 0, 4) };
        bool first = true;
        while (i < end)
        {
            string line = lines[i];
            if (string.IsNullOrWhiteSpace(line)) break;
            if (!first && IsBlockStart(lines, i, end)) break;
            if (!first) p.Inlines.Add(new LineBreak());
            AddInlines(p.Inlines, line.Trim());
            first = false;
            i++;
        }
        return p;
    }

    private static Block BuildHeading(int level, string text, Brush fg)
    {
        double size = level switch
        {
            1 => 22,
            2 => 19,
            3 => 17,
            4 => 15.5,
            5 => 14.5,
            _ => 13.5,
        };
        var p = new Paragraph
        {
            FontSize = size,
            FontWeight = FontWeights.SemiBold,
            Margin = new Thickness(0, level <= 2 ? 8 : 6, 0, 4),
        };
        AddInlines(p.Inlines, text);
        return p;
    }

    private static Block BuildCodeBlock(string code)
    {
        var p = new Paragraph
        {
            FontFamily = MonoFont,
            FontSize = 13,
            Background = CodeBlockBg,
            BorderBrush = SoftBorder,
            BorderThickness = new Thickness(1),
            Padding = new Thickness(10, 8, 10, 8),
            Margin = new Thickness(0, 4, 0, 6),
        };
        string[] parts = code.Split('\n');
        for (int k = 0; k < parts.Length; k++)
        {
            if (k > 0) p.Inlines.Add(new LineBreak());
            p.Inlines.Add(new Run(parts[k]));
        }
        return p;
    }

    private static Block BuildHorizontalRule()
    {
        var rule = new System.Windows.Controls.Border
        {
            Height = 1,
            Background = SoftBorder,
            Margin = new Thickness(0, 6, 0, 6),
            HorizontalAlignment = HorizontalAlignment.Stretch,
        };
        return new BlockUIContainer(rule) { Margin = new Thickness(0) };
    }

    private static Block BuildBlockquote(List<string> innerLines, Brush fg)
    {
        var section = new Section
        {
            BorderBrush = AccentBrush,
            BorderThickness = new Thickness(3, 0, 0, 0),
            Padding = new Thickness(10, 2, 0, 2),
            Margin = new Thickness(0, 4, 0, 6),
            Foreground = MutedFg,
        };
        string[] arr = innerLines.ToArray();
        int j = 0;
        ParseBlocks(arr, ref j, arr.Length, section.Blocks, MutedFg);
        if (section.Blocks.Count == 0)
            section.Blocks.Add(new Paragraph { Margin = new Thickness(0) });
        return section;
    }

    private static Block BuildList(string[] lines, ref int i, int end, int baseIndent, Brush fg)
    {
        IsListItem(lines[i], out _, out bool ordered, out int start, out _);
        var list = new DocList
        {
            Margin = new Thickness(0, 2, 0, 4),
            Padding = new Thickness(0),
            MarkerStyle = ordered ? TextMarkerStyle.Decimal : TextMarkerStyle.Disc,
        };
        if (ordered) list.StartIndex = Math.Max(1, start);

        ListItem? lastItem = null;
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

            // Deeper indentation belongs to a nested list under the previous item.
            if (indent >= baseIndent + 2 && lastItem is not null)
            {
                var nested = BuildList(lines, ref i, end, indent, fg);
                lastItem.Blocks.Add(nested);
                continue;
            }

            i++;
            var li = new ListItem();
            var p = new Paragraph { Margin = new Thickness(0, 1, 0, 1) };
            AddInlines(p.Inlines, content.Trim());
            li.Blocks.Add(p);
            list.ListItems.Add(li);
            lastItem = li;
        }

        return list;
    }

    private static Block BuildTable(string[] lines, ref int i, int end, Brush fg)
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

        var table = new Table
        {
            CellSpacing = 0,
            BorderBrush = SoftBorder,
            BorderThickness = new Thickness(1, 1, 0, 0),
            Margin = new Thickness(0, 4, 0, 6),
        };
        for (int c = 0; c < cols; c++)
            table.Columns.Add(new TableColumn { Width = new GridLength(1, GridUnitType.Star) });

        var group = new TableRowGroup();

        var headerRow = new TableRow { Background = TableHeaderBg };
        for (int c = 0; c < cols; c++)
            headerRow.Cells.Add(BuildCell(c < header.Count ? header[c] : "", AlignAt(aligns, c), bold: true));
        group.Rows.Add(headerRow);

        foreach (var row in bodyRows)
        {
            var tr = new TableRow();
            for (int c = 0; c < cols; c++)
                tr.Cells.Add(BuildCell(c < row.Count ? row[c] : "", AlignAt(aligns, c), bold: false));
            group.Rows.Add(tr);
        }

        table.RowGroups.Add(group);
        return table;
    }

    private static TableCell BuildCell(string text, TextAlignment align, bool bold)
    {
        var p = new Paragraph { Margin = new Thickness(0), TextAlignment = align };
        if (bold) p.FontWeight = FontWeights.SemiBold;
        AddInlines(p.Inlines, text);
        return new TableCell(p)
        {
            BorderBrush = SoftBorder,
            BorderThickness = new Thickness(0, 0, 1, 1),
            Padding = new Thickness(8, 4, 8, 4),
        };
    }

    // ---------------- Block detection helpers ----------------

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
        // Strip an optional closing '#' sequence (e.g. "## Title ##"), but keep a
        // legitimate trailing '#' like "C#".
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
                     : right ? TextAlignment.Right
                     : TextAlignment.Left);
        }
        return result;
    }

    private static TextAlignment AlignAt(List<TextAlignment> aligns, int index)
        => index < aligns.Count ? aligns[index] : TextAlignment.Left;

    // ---------------- Inline parsing ----------------

    private static void AddInlines(InlineCollection target, string text)
    {
        foreach (var inline in ParseInlines(text, 0))
            target.Add(inline);
    }

    private static List<Inline> ParseInlines(string text, int depth)
    {
        var result = new List<Inline>();
        if (string.IsNullOrEmpty(text)) return result;
        if (depth > MaxInlineDepth) { result.Add(new Run(text)); return result; }

        var sb = new StringBuilder();
        int i = 0;
        int n = text.Length;

        void Flush()
        {
            if (sb.Length > 0) { result.Add(new Run(sb.ToString())); sb.Clear(); }
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
                    result.Add(MakeInlineCode(text.Substring(i + run, close - (i + run))));
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
                    result.Add(BuildHyperlink(lk.Text, lk.Url, depth));
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
                    Span span = markerLen == 2 ? new Bold() : new Italic();
                    foreach (var inl in ParseInlines(inner, depth + 1)) span.Inlines.Add(inl);
                    result.Add(span);
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

    private static Inline MakeInlineCode(string code)
    {
        // CommonMark: drop one leading and trailing space when both are present.
        if (code.Length >= 2 && code[0] == ' ' && code[^1] == ' ' && code.Trim().Length > 0)
            code = code[1..^1];
        return new Run(code) { FontFamily = MonoFont, Background = InlineCodeBg };
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

    private static Hyperlink BuildHyperlink(string label, string url, int depth)
    {
        var h = new Hyperlink { Foreground = AccentBrush, Cursor = Cursors.Hand };
        foreach (var inl in ParseInlines(label, depth + 1)) h.Inlines.Add(inl);
        if (h.Inlines.Count == 0) h.Inlines.Add(new Run(string.IsNullOrEmpty(url) ? label : url));

        if (Uri.TryCreate(url, UriKind.Absolute, out var uri) && IsSafeScheme(uri))
        {
            h.NavigateUri = uri;
            h.RequestNavigate += OnRequestNavigate;
        }
        if (!string.IsNullOrEmpty(url)) h.ToolTip = url;
        return h;
    }

    private static bool IsSafeScheme(Uri uri) =>
        uri.Scheme == Uri.UriSchemeHttp ||
        uri.Scheme == Uri.UriSchemeHttps ||
        uri.Scheme == Uri.UriSchemeMailto;

    private static void OnRequestNavigate(object sender, RequestNavigateEventArgs e)
    {
        try
        {
            if (e.Uri is { } uri && IsSafeScheme(uri))
                Process.Start(new ProcessStartInfo(uri.AbsoluteUri) { UseShellExecute = true });
        }
        catch
        {
            // Opening the browser is best-effort; never crash the chat UI.
        }
        e.Handled = true;
    }

    private static Brush Frozen(byte r, byte g, byte b)
    {
        var brush = new SolidColorBrush(Color.FromRgb(r, g, b));
        brush.Freeze();
        return brush;
    }
}
