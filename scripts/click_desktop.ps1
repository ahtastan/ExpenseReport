param(
    [Parameter(Mandatory=$true)][int]$X,
    [Parameter(Mandatory=$true)][int]$Y
)

$code = @'
using System;
using System.Runtime.InteropServices;
public static class MouseClicker {
  [DllImport("user32.dll")]
  public static extern bool SetCursorPos(int X, int Y);
  [DllImport("user32.dll")]
  public static extern void mouse_event(int dwFlags, int dx, int dy, int dwData, int dwExtraInfo);
  public const int LEFTDOWN = 0x0002;
  public const int LEFTUP = 0x0004;
}
'@
Add-Type $code
[MouseClicker]::SetCursorPos($X, $Y) | Out-Null
Start-Sleep -Milliseconds 100
[MouseClicker]::mouse_event([MouseClicker]::LEFTDOWN, $X, $Y, 0, 0)
Start-Sleep -Milliseconds 80
[MouseClicker]::mouse_event([MouseClicker]::LEFTUP, $X, $Y, 0, 0)
