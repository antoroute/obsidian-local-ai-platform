param(
    [string]$Models = "qwen2.5:7b,mistral:latest,nomic-embed-text:latest",
    [ValidateSet("gpu", "cpu")]
    [string]$Mode = "gpu",
    [ValidateSet("host", "docker")]
    [string]$Source = "host",
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path

function Get-ProdComposeFiles {
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

function Get-SetupComposeFiles {
    return @("-f", "docker-compose.yml", "-f", "infra/docker-compose.ollama-model-setup.yml")
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

function Get-RequestedModels {
    param([string]$RawModels)

    return $RawModels.Split(",") |
        ForEach-Object { $_.Trim() } |
        Where-Object { $_ }
}

function Get-OllamaManifestParts {
    param([string]$Model)

    $name = $Model
    $tag = "latest"
    if ($Model.Contains(":")) {
        $separatorIndex = $Model.LastIndexOf(":")
        $name = $Model.Substring(0, $separatorIndex)
        $tag = $Model.Substring($separatorIndex + 1)
    }

    $parts = @($name.Split("/") | Where-Object { $_ })
    if ($parts.Count -eq 1) {
        return @{
            Registry = "registry.ollama.ai"
            Namespace = "library"
            Name = $parts[0]
            Tag = $tag
        }
    }
    if ($parts.Count -eq 2) {
        return @{
            Registry = "registry.ollama.ai"
            Namespace = $parts[0]
            Name = $parts[1]
            Tag = $tag
        }
    }

    return @{
        Registry = $parts[0]
        Namespace = $parts[1]
        Name = ($parts[2..($parts.Count - 1)] -join "/")
        Tag = $tag
    }
}

function Get-HostManifestPath {
    param(
        [string]$Store,
        [hashtable]$Parts
    )

    return Join-Path $Store (Join-Path "manifests" (Join-Path $Parts["Registry"] (Join-Path $Parts["Namespace"] (Join-Path $Parts["Name"] $Parts["Tag"]))))
}

function Get-ContainerManifestDir {
    param([hashtable]$Parts)

    return "/root/.ollama/models/manifests/$($Parts["Registry"])/$($Parts["Namespace"])/$($Parts["Name"])"
}

function Get-ManifestDigests {
    param([string]$ManifestPath)

    $manifestText = Get-Content -Raw $ManifestPath
    $matches = [regex]::Matches($manifestText, "sha256:[a-fA-F0-9]{32,}")
    return $matches | ForEach-Object { $_.Value.ToLowerInvariant() } | Select-Object -Unique
}

function Assert-HostModelAvailable {
    param(
        [string]$Model,
        [string]$ManifestPath
    )

    if (-not (Test-Path $ManifestPath)) {
        throw "Host Ollama model '$Model' is missing. Run: ollama pull $Model"
    }
}

function Copy-HostModelToContainer {
    param(
        [string]$ContainerId,
        [string]$Store,
        [string]$Model
    )

    $parts = Get-OllamaManifestParts -Model $Model
    $manifestPath = Get-HostManifestPath -Store $Store -Parts $parts
    Assert-HostModelAvailable -Model $Model -ManifestPath $manifestPath

    $containerManifestDir = Get-ContainerManifestDir -Parts $parts
    $containerManifestPath = "$containerManifestDir/$($parts["Tag"])"

    Write-Host "Model requested: $Model" -ForegroundColor Cyan
    Write-Host "Manifest found: $manifestPath" -ForegroundColor Cyan
    Write-Host "Copying manifest for $Model" -ForegroundColor Cyan
    docker exec $ContainerId mkdir -p $containerManifestDir
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to create manifest directory inside Docker Ollama for '$Model'."
    }
    docker cp $manifestPath "${ContainerId}:$containerManifestPath"
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to copy manifest for '$Model'."
    }

    docker exec $ContainerId mkdir -p /root/.ollama/models/blobs
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to create blobs directory inside Docker Ollama."
    }
    $digests = Get-ManifestDigests -ManifestPath $manifestPath
    if (-not $digests -or $digests.Count -eq 0) {
        throw "No sha256 blobs found in manifest for '$Model'."
    }

    $copiedBlobs = 0
    foreach ($digest in $digests) {
        $blobName = $digest.Replace(":", "-")
        $hostBlob = Join-Path $Store (Join-Path "blobs" $blobName)
        if (-not (Test-Path $hostBlob)) {
            throw "Missing host Ollama blob for '$Model': $blobName. Re-run: ollama pull $Model"
        }

        Write-Host "Copying blob $blobName" -ForegroundColor DarkCyan
        docker cp $hostBlob "${ContainerId}:/root/.ollama/models/blobs/$blobName"
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to copy blob for '$Model': $blobName"
        }
        $copiedBlobs += 1
    }
    Write-Host "Blobs copied for ${Model}: $copiedBlobs" -ForegroundColor Green
}

function Assert-DockerModelPresent {
    param(
        [string[]]$ComposeFiles,
        [string]$Model
    )

    $list = docker compose @ComposeFiles exec -T ollama ollama list
    if ($LASTEXITCODE -ne 0) {
        throw "Could not list models inside Docker Ollama."
    }

    $modelNames = $list | Select-Object -Skip 1 | ForEach-Object {
        ($_ -split "\s+")[0]
    }

    if ($modelNames -notcontains $Model) {
        throw "Model '$Model' was not found inside Docker Ollama after preparation."
    }
    Write-Host "Model present in Docker Ollama: $Model OK" -ForegroundColor Green
}

Push-Location $repoRoot
try {
    $requestedModels = @(Get-RequestedModels -RawModels $Models)
    if ($requestedModels.Count -eq 0) {
        throw "No Ollama models requested."
    }

    Write-Host "Preparing Ollama models: $($requestedModels -join ', ')" -ForegroundColor Cyan
    Write-Host "Mode: $Mode" -ForegroundColor Cyan
    Write-Host "Source: $Source" -ForegroundColor Cyan

    if ($Source -eq "docker" -and -not $Force) {
        Write-Host "Docker source will run 'ollama pull' inside the container and requires outbound Internet." -ForegroundColor Yellow
        $confirmation = Read-Host "Type ALLOW-DOCKER-PULL to continue"
        if ($confirmation -ne "ALLOW-DOCKER-PULL") {
            Write-Host "Aborted. No Docker pull was executed." -ForegroundColor Yellow
            exit 1
        }
    }

    if ($Source -eq "docker") {
        $composeFiles = Get-SetupComposeFiles
        docker compose @composeFiles up -d ollama
        Wait-ComposeServiceHealthy -ComposeFiles $composeFiles -Service "ollama"
        foreach ($model in $requestedModels) {
            Write-Host "Pulling $model inside Docker Ollama..." -ForegroundColor Cyan
            docker compose @composeFiles exec ollama ollama pull $model
            if ($LASTEXITCODE -ne 0) {
                throw "Docker Ollama failed to pull '$model'. Check container Internet/DNS access."
            }
        }

        docker compose @composeFiles exec ollama ollama list
        foreach ($model in $requestedModels) {
            Assert-DockerModelPresent -ComposeFiles $composeFiles -Model $model
        }
        Write-Host "Ollama models prepared in Docker volume 'ollama-data'." -ForegroundColor Green
        return
    }

    if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
        throw "Host Ollama CLI was not found. Install Ollama on the host or use -Source docker."
    }

    $hostStore = Join-Path $env:USERPROFILE ".ollama\models"
    if (-not (Test-Path $hostStore)) {
        throw "Host Ollama model store not found: $hostStore"
    }

    Write-Host "Checking requested models on host Ollama..." -ForegroundColor Cyan
    $hostList = ollama list
    Write-Host $hostList
    foreach ($model in $requestedModels) {
        $parts = Get-OllamaManifestParts -Model $model
        $manifestPath = Get-HostManifestPath -Store $hostStore -Parts $parts
        Assert-HostModelAvailable -Model $model -ManifestPath $manifestPath
    }

    $composeFiles = Get-ProdComposeFiles -SelectedMode $Mode
    docker compose @composeFiles up -d ollama
    Wait-ComposeServiceHealthy -ComposeFiles $composeFiles -Service "ollama"
    $containerId = (docker compose @composeFiles ps -q ollama).Trim()
    if (-not $containerId) {
        throw "Docker Ollama container is not running."
    }

    foreach ($model in $requestedModels) {
        Copy-HostModelToContainer -ContainerId $containerId -Store $hostStore -Model $model
    }

    Write-Host "Restarting Docker Ollama to reload copied manifests..." -ForegroundColor Cyan
    docker compose @composeFiles restart ollama
    Wait-ComposeServiceHealthy -ComposeFiles $composeFiles -Service "ollama"

    Write-Host "Verifying Docker Ollama model list..." -ForegroundColor Cyan
    docker compose @composeFiles exec ollama ollama list
    foreach ($model in $requestedModels) {
        Assert-DockerModelPresent -ComposeFiles $composeFiles -Model $model
    }

    Write-Host "Ollama models prepared selectively in Docker volume 'ollama-data'." -ForegroundColor Green
} catch {
    Write-Host ""
    Write-Host "Failed to prepare Ollama models." -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    Write-Host ""
    Write-Host "If a host model is missing, run the matching host command first:" -ForegroundColor Yellow
    foreach ($model in (Get-RequestedModels -RawModels $Models)) {
        Write-Host "ollama pull $model" -ForegroundColor Yellow
    }
    exit 1
} finally {
    Pop-Location
}
