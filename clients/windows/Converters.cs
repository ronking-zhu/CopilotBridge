using System.Globalization;
using System.Windows;
using System.Windows.Data;
using System.Windows.Media;

namespace CopilotBridgeClient;

public class RoleToAlignmentConverter : IValueConverter
{
    public object Convert(object? value, Type targetType, object? parameter, CultureInfo culture)
        => value is Role r
            ? r switch
            {
                Role.User => HorizontalAlignment.Right,
                Role.System => HorizontalAlignment.Center,
                _ => HorizontalAlignment.Left,
            }
            : HorizontalAlignment.Left;

    public object ConvertBack(object? value, Type targetType, object? parameter, CultureInfo culture)
        => throw new NotSupportedException();
}

public class RoleToBrushConverter : IValueConverter
{
    private static readonly Brush User = Freeze(Color.FromRgb(0x0F, 0x6C, 0xBD));
    private static readonly Brush Assistant = Freeze(Color.FromRgb(0xFF, 0xFF, 0xFF));
    private static readonly Brush System = Freeze(Color.FromRgb(0xEC, 0xEC, 0xEC));

    public object Convert(object? value, Type targetType, object? parameter, CultureInfo culture)
        => value is Role r
            ? r switch { Role.User => User, Role.System => System, _ => Assistant }
            : Assistant;

    public object ConvertBack(object? value, Type targetType, object? parameter, CultureInfo culture)
        => throw new NotSupportedException();

    private static Brush Freeze(Color c)
    {
        var b = new SolidColorBrush(c);
        b.Freeze();
        return b;
    }
}

public class RoleToForegroundConverter : IValueConverter
{
    private static readonly Brush Light = Freeze(Color.FromRgb(0xFF, 0xFF, 0xFF));
    private static readonly Brush Dark = Freeze(Color.FromRgb(0x1B, 0x1B, 0x1B));

    public object Convert(object? value, Type targetType, object? parameter, CultureInfo culture)
        => value is Role r && r == Role.User ? Light : Dark;

    public object ConvertBack(object? value, Type targetType, object? parameter, CultureInfo culture)
        => throw new NotSupportedException();

    private static Brush Freeze(Color c)
    {
        var b = new SolidColorBrush(c);
        b.Freeze();
        return b;
    }
}

public class BoolToVisibilityConverter : IValueConverter
{
    public object Convert(object? value, Type targetType, object? parameter, CultureInfo culture)
        => value is true ? Visibility.Visible : Visibility.Collapsed;

    public object ConvertBack(object? value, Type targetType, object? parameter, CultureInfo culture)
        => value is Visibility v && v == Visibility.Visible;
}

public class InverseBoolToVisibilityConverter : IValueConverter
{
    public object Convert(object? value, Type targetType, object? parameter, CultureInfo culture)
        => value is true ? Visibility.Collapsed : Visibility.Visible;

    public object ConvertBack(object? value, Type targetType, object? parameter, CultureInfo culture)
        => value is Visibility v && v != Visibility.Visible;
}
