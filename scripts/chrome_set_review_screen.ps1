$ErrorActionPreference = 'Stop'

$shell = New-Object -ComObject WScript.Shell
if (-not $shell.AppActivate('Google Chrome')) {
    if (-not $shell.AppActivate('Chrome')) {
        throw 'Could not activate Chrome.'
    }
}
Start-Sleep -Milliseconds 300
$shell.SendKeys('^l')
Start-Sleep -Milliseconds 200
$shell.SendKeys("javascript:localStorage.setItem('er_screen','review');location.reload()")
Start-Sleep -Milliseconds 200
$shell.SendKeys('{ENTER}')
Start-Sleep -Seconds 3
