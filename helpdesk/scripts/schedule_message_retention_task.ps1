param(
    [string]$TaskName = "BESTSUPPORT-MessageRetention",
    [string]$Time = "02:00",
    [int]$Days = 180,
    [int]$ClosedDays = 10
)

$ErrorActionPreference = "Stop"

$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$runScript = Join-Path $scriptRoot "run_message_retention.ps1"

if (!(Test-Path $runScript)) {
    throw "Run script not found at $runScript"
}

$taskCommand = "powershell.exe -ExecutionPolicy Bypass -File `"$runScript`" -Days $Days -ClosedDays $ClosedDays"

schtasks /Create /F /SC DAILY /TN $TaskName /TR $taskCommand /ST $Time /RU SYSTEM /RL HIGHEST | Out-Null

Write-Output "Scheduled task '$TaskName' created. It will run daily at $Time."
