using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;

namespace CopilotBridgeClient;

/// <summary>
/// Minimal Windows DPAPI wrapper (CryptProtectData / CryptUnprotectData) so the saved
/// API key is encrypted per Windows user account instead of stored in plain text.
/// Implemented via P/Invoke to avoid any extra NuGet dependency.
/// </summary>
internal static class Dpapi
{
    [StructLayout(LayoutKind.Sequential)]
    private struct DATA_BLOB
    {
        public int cbData;
        public IntPtr pbData;
    }

    [DllImport("crypt32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    private static extern bool CryptProtectData(
        ref DATA_BLOB pDataIn, string? szDataDescr, IntPtr pOptionalEntropy,
        IntPtr pvReserved, IntPtr pPromptStruct, int dwFlags, ref DATA_BLOB pDataOut);

    [DllImport("crypt32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    private static extern bool CryptUnprotectData(
        ref DATA_BLOB pDataIn, IntPtr ppszDataDescr, IntPtr pOptionalEntropy,
        IntPtr pvReserved, IntPtr pPromptStruct, int dwFlags, ref DATA_BLOB pDataOut);

    [DllImport("kernel32.dll")]
    private static extern IntPtr LocalFree(IntPtr hMem);

    private const int CRYPTPROTECT_UI_FORBIDDEN = 0x1;

    public static string Protect(string plain)
    {
        byte[] data = Encoding.UTF8.GetBytes(plain);
        var input = default(DATA_BLOB);
        var output = default(DATA_BLOB);
        IntPtr pin = Marshal.AllocHGlobal(data.Length == 0 ? 1 : data.Length);
        try
        {
            Marshal.Copy(data, 0, pin, data.Length);
            input.cbData = data.Length;
            input.pbData = pin;
            if (!CryptProtectData(ref input, "CopilotBridgeClient", IntPtr.Zero, IntPtr.Zero,
                    IntPtr.Zero, CRYPTPROTECT_UI_FORBIDDEN, ref output))
                throw new Win32Exception(Marshal.GetLastWin32Error());

            byte[] outBytes = new byte[output.cbData];
            Marshal.Copy(output.pbData, outBytes, 0, output.cbData);
            return System.Convert.ToBase64String(outBytes);
        }
        finally
        {
            Marshal.FreeHGlobal(pin);
            if (output.pbData != IntPtr.Zero) LocalFree(output.pbData);
        }
    }

    public static string Unprotect(string protectedBase64)
    {
        byte[] data = System.Convert.FromBase64String(protectedBase64);
        var input = default(DATA_BLOB);
        var output = default(DATA_BLOB);
        IntPtr pin = Marshal.AllocHGlobal(data.Length == 0 ? 1 : data.Length);
        try
        {
            Marshal.Copy(data, 0, pin, data.Length);
            input.cbData = data.Length;
            input.pbData = pin;
            if (!CryptUnprotectData(ref input, IntPtr.Zero, IntPtr.Zero, IntPtr.Zero,
                    IntPtr.Zero, CRYPTPROTECT_UI_FORBIDDEN, ref output))
                throw new Win32Exception(Marshal.GetLastWin32Error());

            byte[] outBytes = new byte[output.cbData];
            Marshal.Copy(output.pbData, outBytes, 0, output.cbData);
            return Encoding.UTF8.GetString(outBytes);
        }
        finally
        {
            Marshal.FreeHGlobal(pin);
            if (output.pbData != IntPtr.Zero) LocalFree(output.pbData);
        }
    }
}
