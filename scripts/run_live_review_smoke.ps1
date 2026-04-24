$ErrorActionPreference = 'Stop'

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$Python = if ($env:PYTHON) {
    $env:PYTHON
} else {
    'C:\Users\CASPER\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
}
$Verify = Join-Path $RepoRoot '.verify_data'
New-Item -ItemType Directory -Force -Path $Verify | Out-Null

$Guid = [guid]::NewGuid().ToString('N')
$DbPath = Join-Path $Verify "live_browser_smoke_$Guid.db"
$OutLog = Join-Path $Verify "live_browser_smoke_$Guid.out.log"
$ErrLog = Join-Path $Verify "live_browser_smoke_$Guid.err.log"
$ChromeLog = Join-Path $Verify "live_browser_smoke_$Guid.chrome.log"
$ChromeProfile = Join-Path ([System.IO.Path]::GetTempPath()) "live_browser_smoke_chrome_$Guid"

$env:DATABASE_URL = "sqlite:///$DbPath"
$env:EXPENSE_STORAGE_ROOT = $Verify
$env:EXPENSE_REPORT_TEMPLATE_PATH = (Resolve-Path (Join-Path $RepoRoot '..\Expense Report Form_Blank.xlsx')).Path
$env:PYTHONPATH = 'backend'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONDONTWRITEBYTECODE = '1'

@'
from datetime import date

from openpyxl import Workbook
from sqlmodel import Session

from app.db import create_db_and_tables, engine
from app.models import StatementImport, StatementTransaction

from pathlib import Path

create_db_and_tables()
with Session(engine) as session:
    imp = StatementImport(source_filename="live_browser_smoke.xlsx", storage_path="(smoke)", row_count=4)
    session.add(imp)
    session.commit()
    session.refresh(imp)
    for idx, (tx_date, supplier, amount) in enumerate(
        [
            (date(2026, 1, 4), "Aat Istanbul Airport S", 550.00),
            (date(2026, 1, 4), "Sbux Ist Otg Poyrazkoy", 220.00),
            (date(2026, 1, 5), "Uber Trip", 415.25),
            (date(2026, 1, 6), "Catirti Tekel", 88.00),
        ],
        start=1,
    ):
        session.add(
            StatementTransaction(
                statement_import_id=imp.id,
                transaction_date=tx_date,
                supplier_raw=supplier,
                supplier_normalized=supplier.lower(),
                local_currency="TRY",
                local_amount=amount,
                source_row_ref=f"smoke-{idx}",
            )
        )
    session.commit()

fixture = Path(".verify_data") / "live_import_statement_smoke.xlsx"
fixture.parent.mkdir(parents=True, exist_ok=True)
wb = Workbook()
ws = wb.active
ws.append(["Tran Date", "Supplier", "Source Amount", "Amount Incl"])
ws.append(["04/01/2026", "Smoke Import Market", "123.45 TRY", 2.85])
wb.save(fixture)
wb.close()
print(f"seeded={engine.url}")
print(f"import_fixture={fixture.resolve()}")
'@ | & $Python -X utf8 -

$ServerInfo = [System.Diagnostics.ProcessStartInfo]::new()
$ServerInfo.FileName = $Python
$ServerInfo.WorkingDirectory = (Join-Path $RepoRoot 'backend')
$ServerInfo.UseShellExecute = $false
$ServerInfo.CreateNoWindow = $true
$ServerInfo.RedirectStandardOutput = $true
$ServerInfo.RedirectStandardError = $true
$ServerInfo.Arguments = '-X utf8 -m uvicorn app.main:app --host 127.0.0.1 --port 8090'
$Server = [System.Diagnostics.Process]::new()
$Server.StartInfo = $ServerInfo
[void]$Server.Start()
$Chrome = $null

try {
    $Ready = $false
    for ($i = 0; $i -lt 40; $i++) {
        try {
            $Status = (Invoke-WebRequest -UseBasicParsing 'http://127.0.0.1:8090/review' -TimeoutSec 1).StatusCode
            if ($Status -eq 200) {
                $Ready = $true
                break
            }
        } catch {
            Start-Sleep -Milliseconds 500
        }
    }
    if (-not $Ready) {
        throw "server_not_ready stderr=$(Get-Content $ErrLog -Raw)"
    }

    $ChromePath = 'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'
    if (-not (Test-Path $ChromePath)) {
        throw "chrome_not_found at $ChromePath"
    }
    $ChromeInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $ChromeInfo.FileName = $ChromePath
    $ChromeInfo.Arguments = "--no-first-run --no-default-browser-check --no-sandbox --disable-gpu --disable-dev-shm-usage --disable-crash-reporter --disable-crashpad --disable-breakpad --disable-site-isolation-trials --disable-features=Crashpad,NetworkServiceSandbox,RendererCodeIntegrity --remote-debugging-port=9223 --user-data-dir=`"$ChromeProfile`" about:blank"
    $ChromeInfo.UseShellExecute = $false
    $ChromeInfo.CreateNoWindow = $true
    $ChromeInfo.RedirectStandardError = $true
    $ChromeInfo.RedirectStandardOutput = $true
    $Chrome = [System.Diagnostics.Process]::new()
    $Chrome.StartInfo = $ChromeInfo
    [void]$Chrome.Start()

    $DevToolsWs = ''
    $ChromeLines = [System.Collections.Generic.List[string]]::new()
    for ($i = 0; $i -lt 40; $i++) {
        $LineTask = $Chrome.StandardError.ReadLineAsync()
        if ($LineTask.Wait(500)) {
            $Line = $LineTask.Result
            if ($Line) {
                $ChromeLines.Add($Line)
                if ($Line -like 'DevTools listening on ws://*') {
                    $DevToolsWs = ($Line -replace '^DevTools listening on ', '').Trim()
                    break
                }
            }
        }
        if ($Chrome.HasExited) {
            break
        }
    }
    if (-not $DevToolsWs) {
        $ChromeLines | Set-Content -Encoding utf8 $ChromeLog
        throw "devtools_ws_not_found"
    }

    $env:NODE_PATH = 'C:\Users\CASPER\.openclaw\workspace\node_modules'
    $env:SMOKE_BASE_URL = 'http://127.0.0.1:8090'
    $env:SMOKE_ISOLATED = '0'
    $env:SMOKE_CDP_URL = $DevToolsWs
    $env:SMOKE_RAW_CDP = '1'
    node (Join-Path $RepoRoot 'scripts\live_review_smoke.js')
    if ($LASTEXITCODE -ne 0) {
        throw "node_smoke_failed exit=$LASTEXITCODE"
    }
} finally {
    if ($Chrome -and -not $Chrome.HasExited) {
        $Chrome.Kill()
        $Chrome.WaitForExit()
    }
    $ChromePort = Get-NetTCPConnection -LocalPort 9223 -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($ChromePort -and $ChromePort.OwningProcess -gt 0) {
        Stop-Process -Id $ChromePort.OwningProcess
    }
    if ($Chrome) {
        $ChromeLines | Set-Content -Encoding utf8 $ChromeLog
    }
    if ($Server -and -not $Server.HasExited) {
        $Server.Kill()
        $Server.WaitForExit()
    }
    $Server.StandardOutput.ReadToEnd() | Set-Content -Encoding utf8 $OutLog
    $Server.StandardError.ReadToEnd() | Set-Content -Encoding utf8 $ErrLog
}

Write-Output "server_stdout=$OutLog"
Write-Output "server_stderr=$ErrLog"
Write-Output "chrome_log=$ChromeLog"
