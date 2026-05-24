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

function Invoke-ComposeExec {
    param(
        [string[]]$ComposeFiles,
        [string]$Service,
        [string[]]$Command
    )

    docker compose @ComposeFiles exec $Service @Command
}

function Assert-ServiceHasNoPublishedPort {
    param(
        [string[]]$ComposeFiles,
        [string]$Service,
        [int]$Port
    )

    try {
        $result = docker compose @ComposeFiles port $Service $Port 2>$null
        if ($LASTEXITCODE -eq 0 -and $result) {
            throw "$Service unexpectedly exposes port ${Port}: $result"
        }
    } catch {
        if ($_.Exception.Message -notlike "*No public port*") {
            throw
        }
    }
}

function Get-WorkerEnv {
    param(
        [string[]]$ComposeFiles,
        [string]$Name
    )

    return (Invoke-ComposeExec -ComposeFiles $ComposeFiles -Service "whisper-worker" -Command @("python", "-c", "import os; print(os.getenv('$Name', 'unset'))")).Trim()
}

function Assert-WorkerEnv {
    param(
        [string[]]$ComposeFiles,
        [string]$Name,
        [string]$Expected
    )

    $actual = Get-WorkerEnv -ComposeFiles $ComposeFiles -Name $Name
    if ($actual -ne $Expected) {
        throw "$Name is '$actual' instead of '$Expected'."
    }
    Write-Host "OK whisper-worker $Name=$Expected" -ForegroundColor Green
}

function Test-GpuWorkerRuntime {
    param([string[]]$ComposeFiles)

    Write-Host "Checking GPU runtime inside whisper-worker..." -ForegroundColor Cyan
    docker compose @ComposeFiles exec whisper-worker nvidia-smi
    if ($LASTEXITCODE -ne 0) {
        throw "nvidia-smi is not accessible inside whisper-worker. Check NVIDIA Container Toolkit, host GPU drivers, and compose GPU settings."
    }
    Write-Host "OK whisper-worker nvidia-smi" -ForegroundColor Green

    docker compose @ComposeFiles exec whisper-worker sh -lc "ldconfig -p 2>/dev/null | grep -q 'libcublas.so.12' || find /usr/local /usr/lib -name 'libcublas.so.12*' -print -quit | grep -q ."
    if ($LASTEXITCODE -ne 0) {
        throw "libcublas.so.12 is missing inside whisper-worker. Rebuild with apps/whisper-worker/Dockerfile.gpu based on nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04."
    }
    Write-Host "OK whisper-worker libcublas.so.12" -ForegroundColor Green
}

Push-Location $repoRoot
try {
    $composeFiles = Get-ComposeFiles -SelectedMode $Mode

    Write-Host "Checking Gateway health..." -ForegroundColor Cyan
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:8000/v1/health" -TimeoutSec 10
    if ($health.status -ne "ok") {
        throw "Gateway healthcheck did not return status=ok."
    }
    Write-Host "OK Gateway health" -ForegroundColor Green

    $llmProvider = (Invoke-ComposeExec -ComposeFiles $composeFiles -Service "ai-gateway" -Command @("printenv", "LLM_PROVIDER")).Trim()
    if ($llmProvider -ne "ollama") {
        throw "LLM_PROVIDER is '$llmProvider' instead of 'ollama'."
    }
    Write-Host "OK Gateway LLM_PROVIDER=ollama" -ForegroundColor Green

    $ollamaBaseUrl = (Invoke-ComposeExec -ComposeFiles $composeFiles -Service "ai-gateway" -Command @("printenv", "OLLAMA_BASE_URL")).Trim()
    if ($ollamaBaseUrl -ne "http://ollama:11434") {
        throw "OLLAMA_BASE_URL is '$ollamaBaseUrl' instead of 'http://ollama:11434'."
    }
    Write-Host "OK Gateway OLLAMA_BASE_URL=http://ollama:11434" -ForegroundColor Green

    Write-Host "Checking Ollama from ai-gateway..." -ForegroundColor Cyan
    docker compose @composeFiles exec ai-gateway python -m app.cli check-ollama --model mistral:latest
    Write-Host "OK Ollama" -ForegroundColor Green

    Assert-WorkerEnv -ComposeFiles $composeFiles -Name "TRANSCRIPTION_ENGINE" -Expected "faster_whisper"

    if ($Mode -eq "gpu") {
        Assert-WorkerEnv -ComposeFiles $composeFiles -Name "WHISPER_DEVICE" -Expected "cuda"
        Assert-WorkerEnv -ComposeFiles $composeFiles -Name "WHISPER_COMPUTE_TYPE" -Expected "float16"
        Test-GpuWorkerRuntime -ComposeFiles $composeFiles
    } else {
        Assert-WorkerEnv -ComposeFiles $composeFiles -Name "WHISPER_DEVICE" -Expected "cpu"
        Assert-WorkerEnv -ComposeFiles $composeFiles -Name "WHISPER_COMPUTE_TYPE" -Expected "int8"
    }

    Write-Host "Checking whisper-worker engine..." -ForegroundColor Cyan
    docker compose @composeFiles exec whisper-worker python -m whisper_worker check-engine
    Write-Host "OK Whisper" -ForegroundColor Green

    $gatewayPort = docker compose @composeFiles port ai-gateway 8000
    if ($gatewayPort -notlike "127.0.0.1:8000") {
        throw "ai-gateway is not published as 127.0.0.1:8000."
    }

    Assert-ServiceHasNoPublishedPort -ComposeFiles $composeFiles -Service "ollama" -Port 11434
    Assert-ServiceHasNoPublishedPort -ComposeFiles $composeFiles -Service "redis" -Port 6379
    Assert-ServiceHasNoPublishedPort -ComposeFiles $composeFiles -Service "postgres" -Port 5432
    Assert-ServiceHasNoPublishedPort -ComposeFiles $composeFiles -Service "whisper-worker" -Port 8001

    Write-Host "OK Internal services are not exposed publicly" -ForegroundColor Green
} catch {
    Write-Host ""
    Write-Host "Stack diagnostic failed." -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    if ($Mode -eq "gpu") {
        Write-Host "Prod GPU stack is not using faster_whisper cuda. Check compose overrides." -ForegroundColor Red
    } else {
        Write-Host "Prod CPU stack is not using faster_whisper cpu/int8. Check compose overrides." -ForegroundColor Red
    }
    Write-Host ""
    Write-Host "Actionable checks:" -ForegroundColor Yellow
    Write-Host '.\scripts\prod\prepare-whisper-model.ps1 -Model medium -Mode gpu' -ForegroundColor Yellow
    Write-Host 'docker compose -f docker-compose.yml -f infra/docker-compose.ollama-model-setup.yml exec ollama ollama list' -ForegroundColor Yellow
    Write-Host 'docker compose -f docker-compose.yml -f infra/docker-compose.prod.external-proxy.yml -f infra/docker-compose.prod.gpu.yml logs ai-gateway' -ForegroundColor Yellow
    Write-Host 'docker compose -f docker-compose.yml -f infra/docker-compose.prod.external-proxy.yml -f infra/docker-compose.prod.gpu.yml logs whisper-worker' -ForegroundColor Yellow
    exit 1
} finally {
    Pop-Location
}
