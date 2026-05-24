param(
    [ValidateSet("small", "medium", "large-v3")]
    [string]$Model = "medium",
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

    $files += @("-f", "infra/docker-compose.whisper-model-setup.yml")
    return $files
}

Push-Location $repoRoot
try {
    $composeFiles = Get-ComposeFiles -SelectedMode $Mode

    Write-Host "Preparing faster-whisper model '$Model' in $Mode mode..." -ForegroundColor Cyan
    Write-Host "Starting required dependencies..." -ForegroundColor Cyan
    docker compose @composeFiles up -d postgres redis

    Write-Host "Downloading the model into the persistent whisper-model-cache volume..." -ForegroundColor Cyan
    docker compose @composeFiles run --rm whisper-worker python -m whisper_worker prepare-model --model $Model

    Write-Host "Whisper model prepared successfully in Docker volume 'whisper-model-cache'." -ForegroundColor Green
    Write-Host "Model cache path inside the container: /models/whisper" -ForegroundColor Green
} catch {
    Write-Host ""
    Write-Host "Failed to prepare the faster-whisper model." -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    Write-Host "If Hugging Face is inaccessible, verify outbound Internet and DNS access for the worker container." -ForegroundColor Yellow
    exit 1
} finally {
    Pop-Location
}
