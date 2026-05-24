param(
    [ValidateSet("gpu", "cpu")]
    [string]$Mode = "gpu"
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path

function Get-ComposeFiles {
    param([string]$SelectedMode)

    $files = @(
        "-f", "docker-compose.yml",
        "-f", "infra/docker-compose.prod.external-proxy.yml"
    )

    if ($SelectedMode -eq "gpu") {
        $files += @("-f", "infra/docker-compose.prod.gpu.yml")
    } else {
        $files += @("-f", "infra/docker-compose.prod.cpu.yml")
    }

    return $files
}

Push-Location $repoRoot
try {
    $composeFiles = Get-ComposeFiles -SelectedMode $Mode
    Write-Host "Starting production-like stack in $Mode mode..." -ForegroundColor Cyan
    docker compose @composeFiles up -d --build
} finally {
    Pop-Location
}
