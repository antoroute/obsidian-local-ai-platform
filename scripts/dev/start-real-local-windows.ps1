$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$composeArgs = @(
    "-f", "docker-compose.yml",
    "-f", "infra/docker-compose.dev.real-local.override.yml",
    "up", "--build"
)
$defaultModel = "mistral:latest"
$ollamaApiUrl = "http://127.0.0.1:11434/api/tags"

function Test-CommandAvailable {
    param([string]$Name)

    return $null -ne (Get-Command $Name -ErrorAction SilentlyContinue)
}

function Test-OllamaApi {
    try {
        Invoke-RestMethod -Uri $ollamaApiUrl -TimeoutSec 5 | Out-Null
        return $true
    } catch {
        return $false
    }
}

function Wait-OllamaApi {
    param([int]$TimeoutSeconds = 60)

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-OllamaApi) {
            return $true
        }
        Start-Sleep -Seconds 2
    }

    return $false
}

if (-not (Test-CommandAvailable "docker")) {
    throw "Docker is not available in PATH."
}

if (-not (Test-CommandAvailable "ollama")) {
    throw "Ollama is not available in PATH."
}

docker info | Out-Null

Write-Warning "OLLAMA_HOST=0.0.0.0 expose Ollama sur les interfaces reseau de la machine. Utiliser uniquement en developpement local ou proteger avec le firewall."

$existingOllama = Get-Process ollama -ErrorAction SilentlyContinue
if ($existingOllama) {
    Write-Host "An existing Ollama process was detected." -ForegroundColor Yellow
    Write-Host "If host Ollama is not reachable from Docker, stop it and rerun this script:" -ForegroundColor Yellow
    Write-Host 'Get-Process ollama -ErrorAction SilentlyContinue | Stop-Process -Force' -ForegroundColor Yellow
}

if (-not (Test-OllamaApi)) {
    Write-Host "Starting Ollama host service in a new PowerShell window..." -ForegroundColor Cyan
    $command = '$env:OLLAMA_HOST="0.0.0.0:11434"; Write-Host "Starting Ollama with OLLAMA_HOST=$env:OLLAMA_HOST"; ollama serve'
    Start-Process powershell -ArgumentList "-NoExit", "-Command", $command -WindowStyle Normal | Out-Null

    Write-Host "Waiting for Ollama host API..." -ForegroundColor Cyan
    if (-not (Wait-OllamaApi)) {
        throw "Ollama host API did not become ready on $ollamaApiUrl. Check firewall rules and whether Ollama is listening on 0.0.0.0:11434."
    }
} else {
    Write-Host "Ollama host API is already responding on 127.0.0.1:11434." -ForegroundColor Green
}

$tagsPayload = Invoke-RestMethod -Uri $ollamaApiUrl -TimeoutSec 10
$modelNames = @()
if ($tagsPayload.models) {
    $modelNames = @($tagsPayload.models | ForEach-Object { $_.name } | Where-Object { $_ })
}

if ($modelNames -notcontains $defaultModel) {
    Write-Host "Model '$defaultModel' is missing from host Ollama." -ForegroundColor Yellow
    Write-Host "Install it with:" -ForegroundColor Yellow
    Write-Host "ollama pull $defaultModel" -ForegroundColor Yellow
    exit 1
}

Write-Host "Model '$defaultModel' is available in host Ollama." -ForegroundColor Green
Write-Host "Starting real local stack..." -ForegroundColor Cyan
Push-Location $repoRoot
try {
    docker compose @composeArgs
} finally {
    Pop-Location
}
