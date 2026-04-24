param(
    [Parameter(Mandatory=$true)][int]$X,
    [Parameter(Mandatory=$true)][int]$Y
)

$code = @'
using System;
using System.Runtime.InteropServices;
public static class WinUi {
  [DllImport("user32.dll")]
  public static extern bool SetForegroundWindow(IntPtr hWnd);
  [DllImport("user32.dll")]
  public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
  [DllImport("user32.dll")]
  public static extern bool SetCursorPos(int X, int Y);
  [DllImport("user32.dll")]
  public static extern void mouse_event(int dwFlags, int dx, int dy, int dwData, int dwExtraInfo);
  public const int SW_RESTORE = 9;
  public const int LEFTDOWN = 0x0002;
  public const int LEFTUP = 0x0004;
}
'@
Add-Type $code

$chrome = Get-Process chrome -ErrorAction Stop | Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1
if (-not $chrome) {
    throw 'No Chrome window with a main handle was found.'
}
[WinUi]::ShowWindow($chrome.MainWindowHandle, [WinUi]::SW_RESTORE) | Out-Null
[WinUi]::SetForegroundWindow($chrome.MainWindowHandle) | Out-Null
Start-Sleep -Milliseconds 500
[WinUi]::SetCursorPos($X, $Y) | Out-Null
Start-Sleep -Milliseconds 100
[WinUi]::mouse_event([WinUi]::LEFTDOWN, $X, $Y, 0, 0)
Start-Sleep -Milliseconds 100
[WinUi]::mouse_event([WinUi]::LEFTUP, $X, $Y, 0, 0)
Start-Sleep -Seconds 2
