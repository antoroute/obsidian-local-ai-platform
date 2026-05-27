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
        if ($result -and ($result -match "invalid IP:0|No public port|not published|no port")) {
            return
        }
        if ($LASTEXITCODE -eq 0 -and $result) {
            throw "$Service unexpectedly exposes port ${Port}: $result"
        }
    } catch {
        if ($_.Exception.Message -notlike "*No public port*" -and $_.Exception.Message -notlike "*not published*" -and $_.Exception.Message -notlike "*invalid IP:0*" -and $_.Exception.Message -notlike "*no port*") {
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

function Get-GatewayEnv {
    param(
        [string[]]$ComposeFiles,
        [string]$Name
    )

    return (Invoke-ComposeExec -ComposeFiles $ComposeFiles -Service "ai-gateway" -Command @("printenv", $Name)).Trim()
}

function Print-EffectiveRuntimeConfiguration {
    param([string[]]$ComposeFiles)

    Write-Host "Effective runtime configuration" -ForegroundColor Cyan
    foreach ($name in @("LLM_PROVIDER", "OLLAMA_BASE_URL", "DEFAULT_MODEL", "ALLOWED_MODELS", "RAG_VECTOR_BACKEND", "RAG_EMBEDDING_MODEL", "RAG_EMBEDDING_DIMENSION")) {
        $value = Get-GatewayEnv -ComposeFiles $ComposeFiles -Name $name
        Write-Host "ai-gateway $name=$value"
    }
    foreach ($name in @("TRANSCRIPTION_ENGINE", "WHISPER_MODEL_SIZE", "WHISPER_DEVICE", "WHISPER_COMPUTE_TYPE", "WHISPER_LANGUAGE", "WHISPER_MODEL_CACHE_DIR")) {
        $value = Get-WorkerEnv -ComposeFiles $ComposeFiles -Name $name
        Write-Host "whisper-worker $name=$value"
    }
    Write-Host ""
}

function Get-GatewayHealthUrls {
    param([string[]]$ComposeFiles)

    if ($env:GATEWAY_BIND_ADDRESS -and $env:GATEWAY_BIND_PORT) {
        $hostAddress = $env:GATEWAY_BIND_ADDRESS
        if ($hostAddress -eq "0.0.0.0") {
            $hostAddress = "127.0.0.1"
        }
        return @("http://${hostAddress}:$($env:GATEWAY_BIND_PORT)/v1/health")
    }

    $published = docker compose @ComposeFiles port ai-gateway 8000 2>$null
    if ($LASTEXITCODE -eq 0 -and $published) {
        return @("http://$published/v1/health", "http://127.0.0.1:8000/v1/health")
    }

    return @("http://127.0.0.1:8000/v1/health")
}

function Test-GatewayHealthFromHost {
    param([string[]]$ComposeFiles)

    $urls = Get-GatewayHealthUrls -ComposeFiles $ComposeFiles
    $lastError = $null
    foreach ($url in $urls) {
        Write-Host "Checking Gateway health at $url ..." -ForegroundColor Cyan
        try {
            $health = Invoke-RestMethod -Uri $url -TimeoutSec 10
            if ($health.status -eq "ok") {
                Write-Host "OK Gateway health at $url" -ForegroundColor Green
                return
            }
            $lastError = "Gateway health at $url did not return status=ok."
        } catch {
            $lastError = $_.Exception.Message
        }
    }

    Write-Host "Host health check failed. Checking health inside ai-gateway container..." -ForegroundColor Yellow
    docker compose @ComposeFiles exec ai-gateway python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/v1/health', timeout=10).read().decode())"
    if ($LASTEXITCODE -eq 0) {
        throw "Gateway is healthy inside container but not reachable from host. Check ports/GATEWAY_BIND_ADDRESS/firewall. Last host error: $lastError"
    }
    throw "Gateway health failed from host and inside container. Last host error: $lastError"
}

function Get-OllamaModels {
    param([string[]]$ComposeFiles)

    $list = docker compose @ComposeFiles exec -T ollama ollama list
    if ($LASTEXITCODE -ne 0) {
        throw "Could not list models inside Docker Ollama."
    }

    Write-Host "Docker Ollama models:" -ForegroundColor Cyan
    $list | ForEach-Object { Write-Host $_ }

    return @($list | Select-Object -Skip 1 | ForEach-Object {
        ($_ -split "\s+")[0]
    } | Where-Object { $_ })
}

function Assert-OllamaModelPresent {
    param(
        [string[]]$Models,
        [string]$Model,
        [string]$MissingMessage
    )

    if ($Models -notcontains $Model) {
        throw $MissingMessage
    }
    Write-Host "OK Docker Ollama model present: $Model" -ForegroundColor Green
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
    Print-EffectiveRuntimeConfiguration -ComposeFiles $composeFiles

    Test-GatewayHealthFromHost -ComposeFiles $composeFiles

    $llmProvider = Get-GatewayEnv -ComposeFiles $composeFiles -Name "LLM_PROVIDER"
    if ($llmProvider -ne "ollama") {
        throw "LLM_PROVIDER is '$llmProvider' instead of 'ollama'."
    }
    Write-Host "OK Gateway LLM_PROVIDER=ollama" -ForegroundColor Green

    $ollamaBaseUrl = Get-GatewayEnv -ComposeFiles $composeFiles -Name "OLLAMA_BASE_URL"
    if ($ollamaBaseUrl -ne "http://ollama:11434") {
        throw "OLLAMA_BASE_URL is '$ollamaBaseUrl' instead of 'http://ollama:11434'."
    }
    Write-Host "OK Gateway OLLAMA_BASE_URL=http://ollama:11434" -ForegroundColor Green

    $defaultModel = Get-GatewayEnv -ComposeFiles $composeFiles -Name "DEFAULT_MODEL"
    Write-Host "Checking Ollama from ai-gateway with DEFAULT_MODEL=$defaultModel..." -ForegroundColor Cyan
    docker compose @composeFiles exec ai-gateway python -m app.cli check-ollama --model $defaultModel
    if ($LASTEXITCODE -ne 0) {
        throw "Ollama connectivity or model check failed from ai-gateway."
    }
    Write-Host "OK Ollama" -ForegroundColor Green

    $ollamaModels = Get-OllamaModels -ComposeFiles $composeFiles
    Assert-OllamaModelPresent -Models $ollamaModels -Model $defaultModel -MissingMessage "Default LLM model missing. Run scripts/prod/prepare-ollama-models.ps1 -Models 'qwen2.5:7b,mistral:latest,nomic-embed-text:latest'"
    Assert-OllamaModelPresent -Models $ollamaModels -Model "nomic-embed-text:latest" -MissingMessage "Embedding model missing. Run scripts/prod/prepare-ollama-models.ps1 -Models 'qwen2.5:7b,mistral:latest,nomic-embed-text:latest'"

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
    if ($LASTEXITCODE -ne 0) {
        $configuredWhisperModel = Get-WorkerEnv -ComposeFiles $composeFiles -Name "WHISPER_MODEL_SIZE"
        throw "Whisper model missing. Run scripts/prod/prepare-whisper-model.ps1 -Model $configuredWhisperModel -Mode $Mode"
    }
    Write-Host "OK Whisper" -ForegroundColor Green

    $gatewayPort = docker compose @composeFiles port ai-gateway 8000
    if (-not $gatewayPort) {
        throw "ai-gateway does not publish port 8000 to the host."
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
    if ($_.Exception.Message -match "LLM_PROVIDER|OLLAMA_BASE_URL|TRANSCRIPTION_ENGINE|WHISPER_DEVICE|WHISPER_COMPUTE_TYPE") {
        if ($Mode -eq "gpu") {
            Write-Host "Prod GPU stack is not using faster_whisper cuda. Check compose overrides." -ForegroundColor Red
        } else {
            Write-Host "Prod CPU stack is not using faster_whisper cpu/int8. Check compose overrides." -ForegroundColor Red
        }
    }
    Write-Host ""
    Write-Host "Actionable checks:" -ForegroundColor Yellow
    Write-Host '.\scripts\prod\prepare-ollama-models.ps1 -Models "qwen2.5:7b,mistral:latest,nomic-embed-text:latest" -Mode gpu -Source host' -ForegroundColor Yellow
    Write-Host '.\scripts\prod\prepare-whisper-model.ps1 -Model medium -Mode gpu' -ForegroundColor Yellow
    Write-Host 'docker compose -f docker-compose.yml -f infra/docker-compose.prod.external-proxy.yml -f infra/docker-compose.prod.gpu.yml exec ollama ollama list' -ForegroundColor Yellow
    Write-Host 'docker compose -f docker-compose.yml -f infra/docker-compose.prod.external-proxy.yml -f infra/docker-compose.prod.gpu.yml logs ai-gateway' -ForegroundColor Yellow
    Write-Host 'docker compose -f docker-compose.yml -f infra/docker-compose.prod.external-proxy.yml -f infra/docker-compose.prod.gpu.yml logs whisper-worker' -ForegroundColor Yellow
    exit 1
} finally {
    Pop-Location
}
