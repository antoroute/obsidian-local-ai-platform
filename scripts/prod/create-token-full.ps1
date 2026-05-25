param(
    [ValidateSet("gpu", "cpu")]
    [string]$Mode = "gpu",
    [string]$Name = "note-compagnon-full"
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$fullScopes = "models:list,notes:summarize,audio:transcribe,meetings:generate,assistant:chat,vault:index,vault:search,vault:ask,vault:admin"

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
    Write-Host "Creating full Note Compagnon token with scopes:" -ForegroundColor Cyan
    Write-Host $fullScopes -ForegroundColor Cyan
    Write-Host "The raw token is displayed once only. Store it securely; this script will not write it to a file." -ForegroundColor Yellow

    docker compose @composeFiles exec ai-gateway python -m app.cli create-token --name $Name --scopes $fullScopes
} finally {
    Pop-Location
}
