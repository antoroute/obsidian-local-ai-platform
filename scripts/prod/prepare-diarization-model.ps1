param(
    [string]$Model = "pyannote/speaker-diarization-3.1",
    [ValidateSet("gpu", "cpu")]
    [string]$Mode = "gpu",
    [string]$HuggingFaceToken = ""
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

    Write-Host "Preparing diarization model '$Model' in $Mode mode..." -ForegroundColor Cyan
    Write-Host "Target Docker volume: diarization-model-cache" -ForegroundColor Cyan
    Write-Host "Cache path inside the container: /models/diarization" -ForegroundColor Cyan
    Write-Host "Starting required dependencies..." -ForegroundColor Cyan
    docker compose @composeFiles up -d postgres redis

    Write-Host "Building whisper-worker so the diarization command is available..." -ForegroundColor Cyan
    docker compose @composeFiles build --build-arg INSTALL_DIARIZATION=true whisper-worker
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to build whisper-worker with diarization support."
    }

    $runArgs = @(
        "compose"
    ) + $composeFiles + @(
        "run", "--rm",
        "-e", "DIARIZATION_ENABLED=true",
        "-e", "DIARIZATION_MODEL=$Model",
        "-e", "DIARIZATION_MODEL_CACHE_DIR=/models/diarization",
        "-e", "HF_HOME=/models/diarization",
        "-e", "HUGGINGFACE_HUB_CACHE=/models/diarization/hub"
    )

    if ($HuggingFaceToken) {
        $runArgs += @("-e", "HF_TOKEN=$HuggingFaceToken", "-e", "HUGGINGFACE_HUB_TOKEN=$HuggingFaceToken")
    } elseif ($env:HF_TOKEN) {
        $runArgs += @("-e", "HF_TOKEN=$env:HF_TOKEN", "-e", "HUGGINGFACE_HUB_TOKEN=$env:HF_TOKEN")
    } elseif ($env:HUGGINGFACE_HUB_TOKEN) {
        $runArgs += @("-e", "HF_TOKEN=$env:HUGGINGFACE_HUB_TOKEN", "-e", "HUGGINGFACE_HUB_TOKEN=$env:HUGGINGFACE_HUB_TOKEN")
    }

    $runArgs += @("whisper-worker", "python", "-m", "whisper_worker", "prepare-diarization-model", "--model", $Model)

    Write-Host "Downloading only the requested diarization model into the persistent cache..." -ForegroundColor Cyan
    & docker @runArgs
    if ($LASTEXITCODE -ne 0) {
        throw "Diarization model preparation failed for '$Model'."
    }

    Write-Host "Diarization model prepared successfully in Docker volume 'diarization-model-cache'." -ForegroundColor Green
    Write-Host "Model cache path inside the container: /models/diarization" -ForegroundColor Green
} catch {
    Write-Host ""
    Write-Host "Failed to prepare the diarization model." -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    Write-Host "pyannote models require accepting the Hugging Face terms for pyannote/speaker-diarization-3.1 and pyannote/segmentation-3.0, then passing a token during preparation." -ForegroundColor Yellow
    Write-Host "Retry example: .\scripts\prod\prepare-diarization-model.ps1 -Mode $Mode -Model '$Model' -HuggingFaceToken '<hf_token>'" -ForegroundColor Yellow
    exit 1
} finally {
    Pop-Location
}
