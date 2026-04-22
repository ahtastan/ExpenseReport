$ErrorActionPreference = 'Stop'

Start-Process 'http://127.0.0.1:8080/review'
Start-Sleep -Seconds 3
powershell -ExecutionPolicy Bypass -File 'C:\Users\CASPER\.openclaw\workspace\screenshot.ps1'
Get-Item 'C:\Users\CASPER\.openclaw\workspace\desktop_view.png' | Select-Object FullName, Length, LastWriteTime
