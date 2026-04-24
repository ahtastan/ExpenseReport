param(
    [Parameter(Mandatory=$true)][string]$Keys,
    [int]$WaitSeconds = 2
)

$code = @'
using System;
using System.Runtime.InteropServices;
public static class WinFocus {
  [DllImport("user32.dll")]
  public static extern bool SetForegroundWindow(IntPtr hWnd);
  [DllImport("user32.dll")]
  public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
  public const int SW_RESTORE = 9;
}
'@
Add-Type $code
$chrome = Get-Process chrome -ErrorAction Stop | Where-Object { $_.MainWindowTitle -like '*ExpenseReport*' -and $_.MainWindowHandle -ne 0 } | Select-Object -First 1
if (-not $chrome) {
    throw 'No ExpenseReport Chrome window found.'
}
[WinFocus]::ShowWindow($chrome.MainWindowHandle, [WinFocus]::SW_RESTORE) | Out-Null
[WinFocus]::SetForegroundWindow($chrome.MainWindowHandle) | Out-Null
Start-Sleep -Milliseconds 300
$shell = New-Object -ComObject WScript.Shell
$shell.SendKeys($Keys)
Start-Sleep -Seconds $WaitSeconds
