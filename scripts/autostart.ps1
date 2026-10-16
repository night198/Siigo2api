[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
param(
    [ValidateSet('Install', 'Uninstall')]
    [string] $Action = 'Install',

    [ValidateRange(1, 65535)]
    [int] $Port = 8766
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$root = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$startupDirectory = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup'
$linkPath = Join-Path $startupDirectory 'Siigo2api.lnk'
$startScript = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'start.ps1'))
$pythonPath = Join-Path $root '.venv\Scripts\python.exe'
$powerShellPath = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'

function Convert-ToFullPath([string] $Path) {
    return [System.IO.Path]::GetFullPath($Path)
}

function Test-SamePath([string] $Left, [string] $Right) {
    if ([string]::IsNullOrWhiteSpace($Left) -or [string]::IsNullOrWhiteSpace($Right)) {
        return $false
    }
    return [string]::Equals((Convert-ToFullPath $Left), (Convert-ToFullPath $Right), [System.StringComparison]::OrdinalIgnoreCase)
}

function Get-ExpectedArguments([int] $ShortcutPort) {
    return '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}" -LogToFile -Port {1}' -f $startScript, $ShortcutPort
}

function Get-Shortcut([string] $Path) {
    $shell = New-Object -ComObject WScript.Shell
    try {
        return $shell.CreateShortcut($Path)
    }
    finally {
        [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($shell)
    }
}

function Test-OwnedArguments([string] $Arguments) {
    $pattern = '^' + [regex]::Escape('-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "') + [regex]::Escape($startScript) + [regex]::Escape('" -LogToFile -Port ') + '\d+$'
    return $Arguments -match $pattern
}

function Assert-OwnedShortcut($Shortcut) {
    if (-not (Test-SamePath $Shortcut.TargetPath $powerShellPath) -or
        -not (Test-OwnedArguments $Shortcut.Arguments) -or
        -not (Test-SamePath $Shortcut.WorkingDirectory $root)) {
        throw "Refusing to modify unrelated shortcut '$linkPath': target, arguments, or working directory do not belong to this checkout."
    }
}

if ($Action -eq 'Install') {
    if (-not (Test-Path -LiteralPath $startScript -PathType Leaf)) {
        throw "Required launcher not found: $startScript"
    }
    if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
        throw "Required virtual-environment Python not found: $pythonPath"
    }

    $existing = $null
    if (Test-Path -LiteralPath $linkPath -PathType Leaf) {
        $existing = Get-Shortcut $linkPath
        Assert-OwnedShortcut $existing
        $existingArguments = $existing.Arguments
        if ($existingArguments -ceq (Get-ExpectedArguments $Port)) {
            Write-Output "Startup shortcut already configured: $linkPath"
            exit 0
        }
    }

    $operation = if ($null -eq $existing) { 'Create startup shortcut' } else { 'Update startup shortcut' }
    if ($PSCmdlet.ShouldProcess($linkPath, "$operation for port $Port")) {
        if (-not (Test-Path -LiteralPath $startupDirectory -PathType Container)) {
            New-Item -ItemType Directory -Path $startupDirectory -Force | Out-Null
        }
        $shortcut = if ($null -eq $existing) { Get-Shortcut $linkPath } else { $existing }
        $shortcut.TargetPath = $powerShellPath
        $shortcut.Arguments = Get-ExpectedArguments $Port
        $shortcut.WorkingDirectory = $root
        $shortcut.WindowStyle = 7
        $shortcut.Save()
        Write-Output "Startup shortcut installed: $linkPath"
    }
    exit 0
}

if (-not (Test-Path -LiteralPath $linkPath -PathType Leaf)) {
    Write-Output "Startup shortcut not present: $linkPath"
    exit 0
}

$shortcut = Get-Shortcut $linkPath
Assert-OwnedShortcut $shortcut

if ($PSCmdlet.ShouldProcess($linkPath, 'Recycle startup shortcut')) {
    Add-Type -AssemblyName Microsoft.VisualBasic
    [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile($linkPath, [Microsoft.VisualBasic.FileIO.UIOption]::OnlyErrorDialogs, [Microsoft.VisualBasic.FileIO.RecycleOption]::SendToRecycleBin)
    Write-Output "Startup shortcut removed: $linkPath"
}
