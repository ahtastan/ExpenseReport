$ErrorActionPreference = 'Stop'

$shell = New-Object -ComObject WScript.Shell
$activated = $shell.AppActivate('Google Chrome')
if (-not $activated) {
    $activated = $shell.AppActivate('Chrome')
}
if (-not $activated) {
    throw 'Could not activate an existing Chrome window.'
}

Start-Sleep -Milliseconds 500
$shell.SendKeys('^l')
Start-Sleep -Milliseconds 200
$shell.SendKeys('http://127.0.0.1:8080/review')
Start-Sleep -Milliseconds 200
$shell.SendKeys('{ENTER}')
Start-Sleep -Seconds 5
powershell -ExecutionPolicy Bypass -File 'C:\Users\CASPER\.openclaw\workspace\screenshot.ps1'
Get-Item 'C:\Users\CASPER\.openclaw\workspace\desktop_view.png' | Select-Object FullName, Length, LastWriteTime
