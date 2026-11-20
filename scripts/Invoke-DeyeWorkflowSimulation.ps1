[CmdletBinding()]
param([string]$PythonPath)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
if (-not $PythonPath) {
    $PythonPath = Join-Path $repoRoot '.venv\Scripts\python.exe'
}
$pythonExecutable = (Get-Command $PythonPath -CommandType Application -ErrorAction Stop).Source

Push-Location -LiteralPath $repoRoot
try {
    foreach ($suite in 'test_daily_collection.py', 'test_recommend_deye.py', 'test_approve_deye.py') {
        Write-Host "Simulating $suite"
        & $pythonExecutable -B -m unittest discover -s tests -p $suite -v
        if ($LASTEXITCODE -ne 0) {
            throw "$suite failed with exit code $LASTEXITCODE."
        }
    }
} finally {
    Pop-Location
}
