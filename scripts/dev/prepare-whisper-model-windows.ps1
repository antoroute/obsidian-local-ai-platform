$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$composeBaseArgs = @(
    "-f", "docker-compose.yml",
    "-f", "infra/docker-compose.dev.real-local.override.yml"
)
$modelSize = if ($env:WHISPER_MODEL_SIZE) { $env:WHISPER_MODEL_SIZE } else { "medium" }

Push-Location $repoRoot
try {
    Write-Host "Preparing faster-whisper model cache for model '$modelSize'..." -ForegroundColor Cyan
    Write-Host "Starting required dependencies..." -ForegroundColor Cyan
    docker compose @composeBaseArgs up -d postgres redis

    Write-Host "Downloading the model into the persistent whisper-model-cache volume..." -ForegroundColor Cyan
    docker compose @composeBaseArgs run --rm whisper-worker python -m whisper_worker prepare-model --model $modelSize

    Write-Host "Model cache is stored in the Docker volume mounted at /models/whisper." -ForegroundColor Green
} catch {
    Write-Host ""
    Write-Host "Failed to prepare the faster-whisper model." -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    Write-Host ""
    Write-Host "If Hugging Face is unreachable, check worker DNS/outbound access and retry." -ForegroundColor Yellow
    exit 1
} finally {
    Pop-Location
}
