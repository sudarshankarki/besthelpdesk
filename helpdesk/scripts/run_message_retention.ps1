param(
    [int]$Days = 180,
    [int]$ClosedDays = 10,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Split-Path -Parent $scriptRoot

Push-Location $projectRoot
try {
    $pythonExe = Join-Path $projectRoot "..\\venv\\Scripts\\python.exe"
    $managePy = Join-Path $projectRoot "manage.py"

    if (!(Test-Path $pythonExe)) {
        throw "Python executable not found at $pythonExe"
    }
    if (!(Test-Path $managePy)) {
        throw "manage.py not found at $managePy"
    }

    $args = @($managePy, "prune_ticket_messages", "--days", $Days)
    if ($DryRun) {
        $args += "--dry-run"
    }

    & $pythonExe $args

    $closedArgs = @($managePy, "purge_closed_ticket_conversations", "--days", $ClosedDays)
    if ($DryRun) {
        $closedArgs += "--dry-run"
    }

    & $pythonExe $closedArgs
}
finally {
    Pop-Location
}
