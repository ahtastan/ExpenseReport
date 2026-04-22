$ErrorActionPreference = 'Stop'

$OutputPath = Join-Path (Resolve-Path '.verify_data').Path ('desktop_capture_' + (Get-Date -Format 'yyyyMMdd_HHmmss') + '.png')
Add-Type -AssemblyName System.Windows.Forms,System.Drawing
$Screen = [System.Windows.Forms.Screen]::PrimaryScreen
$Bitmap = New-Object System.Drawing.Bitmap($Screen.Bounds.Width, $Screen.Bounds.Height)
$Graphics = [System.Drawing.Graphics]::FromImage($Bitmap)
$Graphics.CopyFromScreen(0, 0, 0, 0, $Bitmap.Size)
$Bitmap.Save($OutputPath, [System.Drawing.Imaging.ImageFormat]::Png)
$Graphics.Dispose()
$Bitmap.Dispose()
Get-Item $OutputPath | Select-Object FullName, Length, LastWriteTime
