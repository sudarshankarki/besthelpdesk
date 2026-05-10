param(
    [Parameter(Mandatory = $true)]
    [string]$ImageRepository,

    [Parameter(Mandatory = $true)]
    [string]$ImageTag
)

$ErrorActionPreference = "Stop"

$kustomizationPath = Join-Path $PSScriptRoot "kustomization.yaml"

if (!(Test-Path $kustomizationPath)) {
    throw "kustomization.yaml not found at $kustomizationPath"
}

$lines = Get-Content -Path $kustomizationPath
$updated = @()
$newNameSet = $false
$newTagSet = $false

foreach ($line in $lines) {
    if ($line -match '^\s*newName:\s*') {
        $updated += "    newName: $ImageRepository"
        $newNameSet = $true
        continue
    }

    if ($line -match '^\s*newTag:\s*') {
        $updated += "    newTag: $ImageTag"
        $newTagSet = $true
        continue
    }

    $updated += $line
}

if (!($newNameSet -and $newTagSet)) {
    throw "Could not find both newName and newTag entries in $kustomizationPath"
}

Set-Content -Path $kustomizationPath -Value $updated

Write-Output "Updated k8s image to ${ImageRepository}:${ImageTag}"
