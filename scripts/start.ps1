param(
    [ValidateRange(1, 65535)]
    [int] $Port = 8766,
    [switch] $LogToFile
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $root
$env:PYTHONUTF8 = '1'
$env:PYTHONUNBUFFERED = '1'

$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    [Console]::Error.WriteLine("Missing virtual environment Python: $python")
    [Console]::Error.WriteLine('Create the environment and install the project before starting the bridge.')
    exit 1
}

$arguments = @('-m', 'siigo_bridge', 'serve', '--port', [string]$Port)
if ($LogToFile) {
    $logDirectory = Join-Path $env:LOCALAPPDATA 'SiigoApiBridge\logs'
    New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
    $stdoutLog = Join-Path $logDirectory 'bridge.stdout.log'
    $stderrLog = Join-Path $logDirectory 'bridge.stderr.log'
    $process = Start-Process -FilePath $python -ArgumentList $arguments -WorkingDirectory $root -NoNewWindow -RedirectStandardOutput $stdoutLog -RedirectStandardError $stderrLog -Wait -PassThru
} else {
    & $python @arguments
    $exitCode = $LASTEXITCODE
    exit $exitCode
}

exit $process.ExitCode
