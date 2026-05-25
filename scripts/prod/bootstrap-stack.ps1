param(
    [ValidateSet("gpu", "cpu")]
    [string]$Mode = "gpu",
    [switch]$ResetModelCaches,
    [string]$OllamaModels = "mistral:latest,nomic-embed-text:latest",
    [ValidateSet("small", "medium", "large-v3")]
    [string]$WhisperModel = "medium",
    [switch]$SkipWhisper,
    [switch]$SkipOllama,
    [switch]$Force
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

function Wait-ComposeServiceHealthy {
    param(
        [string[]]$ComposeFiles,
        [string]$Service,
        [int]$TimeoutSeconds = 120
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        $containerId = (docker compose @ComposeFiles ps -q $Service).Trim()
        if ($containerId) {
            $health = (docker inspect --format "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}" $containerId 2>$null).Trim()
            if ($health -eq "healthy" -or $health -eq "running") {
                Write-Host "OK $Service is $health" -ForegroundColor Green
                return
            }
            Write-Host "Waiting for $Service health: $health" -ForegroundColor DarkCyan
        }
        Start-Sleep -Seconds 3
    }

    throw "$Service did not become healthy within $TimeoutSeconds seconds."
}

Push-Location $repoRoot
try {
    $composeFiles = Get-ComposeFiles -SelectedMode $Mode

    Write-Host "Note Compagnon production-like bootstrap" -ForegroundColor Cyan
    Write-Host "Mode: $Mode"
    Write-Host "Ollama models: $OllamaModels"
    Write-Host "Whisper model: $WhisperModel"
    Write-Host "Model volumes: ollama-data, whisper-model-cache"
    Write-Host "Protected volumes: postgres-data, audio-storage, vault files"
    Write-Host ""

    if ($ResetModelCaches) {
        Write-Host "Stopping model services before cache reset..." -ForegroundColor Cyan
        docker compose @composeFiles stop ollama whisper-worker
        docker compose @composeFiles rm -f ollama whisper-worker
        if ($Force) {
            & (Join-Path $PSScriptRoot "reset-model-caches.ps1") -Force
        } else {
            & (Join-Path $PSScriptRoot "reset-model-caches.ps1")
        }
    }

    Write-Host "Starting production-like stack..." -ForegroundColor Cyan
    docker compose @composeFiles up -d --build
    Wait-ComposeServiceHealthy -ComposeFiles $composeFiles -Service "ollama"

    if (-not $SkipOllama) {
        if ($Force) {
            & (Join-Path $PSScriptRoot "prepare-ollama-models.ps1") -Mode $Mode -Source host -Models $OllamaModels -Force
        } else {
            & (Join-Path $PSScriptRoot "prepare-ollama-models.ps1") -Mode $Mode -Source host -Models $OllamaModels
        }
    }

    if (-not $SkipWhisper) {
        & (Join-Path $PSScriptRoot "prepare-whisper-model.ps1") -Mode $Mode -Model $WhisperModel
    }

    Write-Host "Running stack diagnostic..." -ForegroundColor Cyan
    & (Join-Path $PSScriptRoot "check-stack.ps1") -Mode $Mode

    Write-Host "Running RAG diagnostic..." -ForegroundColor Cyan
    docker compose @composeFiles exec ai-gateway python -m app.cli check-rag
    if ($LASTEXITCODE -ne 0) {
        throw "RAG diagnostic failed."
    }

    Write-Host ""
    Write-Host "Bootstrap complete." -ForegroundColor Green
    Write-Host "API Base URL for local reverse-proxy testing: http://127.0.0.1:8000"
    Write-Host "Default model: mistral:latest"
    Write-Host ""
    Write-Host "Create a full Note Compagnon token with:" -ForegroundColor Yellow
    Write-Host ".\scripts\prod\create-token-full.ps1 -Mode $Mode -Name note-compagnon-full" -ForegroundColor Yellow
    Write-Host "The token is displayed once only and is never written by the script." -ForegroundColor Yellow
} catch {
    Write-Host ""
    Write-Host "Bootstrap failed." -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
} finally {
    Pop-Location
}
