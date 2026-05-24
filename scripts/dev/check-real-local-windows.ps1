$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$composeBaseArgs = @(
    "-f", "docker-compose.yml",
    "-f", "infra/docker-compose.dev.real-local.override.yml"
)

function Invoke-ComposeExec {
    param(
        [string]$Service,
        [string[]]$Command
    )

    docker compose @composeBaseArgs exec $Service @Command
}

Push-Location $repoRoot
try {
    Write-Host "Checking ai-gateway LLM provider..." -ForegroundColor Cyan
    $llmProvider = (Invoke-ComposeExec -Service "ai-gateway" -Command @("printenv", "LLM_PROVIDER")).Trim()
    Write-Host "LLM_PROVIDER=$llmProvider"
    if ($llmProvider -ne "ollama") {
        throw "ai-gateway is not running with LLM_PROVIDER=ollama."
    }

    Write-Host "Checking whisper-worker transcription engine..." -ForegroundColor Cyan
    $transcriptionEngine = (Invoke-ComposeExec -Service "whisper-worker" -Command @("python", "-c", "import os; print(os.getenv('TRANSCRIPTION_ENGINE', 'unset'))")).Trim()
    Write-Host "TRANSCRIPTION_ENGINE=$transcriptionEngine"
    if ($transcriptionEngine -ne "faster_whisper") {
        throw "whisper-worker is not running with TRANSCRIPTION_ENGINE=faster_whisper."
    }

    Write-Host "Running Ollama connectivity diagnostic from ai-gateway..." -ForegroundColor Cyan
    docker compose @composeBaseArgs exec ai-gateway python -m app.cli check-ollama --model mistral:latest

    Write-Host "Running whisper-worker engine diagnostic..." -ForegroundColor Cyan
    docker compose @composeBaseArgs exec whisper-worker python -m whisper_worker check-engine
} catch {
    Write-Host ""
    Write-Host "Real local diagnostic failed." -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    Write-Host ""
    Write-Host "Recommended follow-up commands:" -ForegroundColor Yellow
    Write-Host '.\scripts\dev\start-real-local-windows.ps1' -ForegroundColor Yellow
    Write-Host 'docker compose -f docker-compose.yml -f infra/docker-compose.dev.real-local.override.yml logs ai-gateway' -ForegroundColor Yellow
    Write-Host 'docker compose -f docker-compose.yml -f infra/docker-compose.dev.real-local.override.yml logs whisper-worker' -ForegroundColor Yellow
    Write-Host 'docker compose -f docker-compose.yml -f infra/docker-compose.dev.real-local.override.yml exec ai-gateway python -m app.cli check-ollama --model mistral:latest' -ForegroundColor Yellow
    Write-Host 'docker compose -f docker-compose.yml -f infra/docker-compose.dev.real-local.override.yml exec whisper-worker python -m whisper_worker check-engine' -ForegroundColor Yellow
    Write-Host '.\scripts\dev\prepare-whisper-model-windows.ps1' -ForegroundColor Yellow
    exit 1
} finally {
    Pop-Location
}

Write-Host "Real local diagnostic OK." -ForegroundColor Green
