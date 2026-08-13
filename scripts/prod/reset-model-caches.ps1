param(
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path

function Get-ComposeProjectName {
    if ($env:COMPOSE_PROJECT_NAME) {
        return $env:COMPOSE_PROJECT_NAME
    }

    $envFile = Join-Path $repoRoot ".env"
    if (Test-Path $envFile) {
        $line = Get-Content $envFile | Where-Object { $_ -match "^\s*COMPOSE_PROJECT_NAME\s*=" } | Select-Object -First 1
        if ($line) {
            return (($line -split "=", 2)[1]).Trim().Trim('"').Trim("'")
        }
    }

    return "obsidian-local-ai-platform"
}

function Get-VolumeUsers {
    param([string]$Name)

    return @(docker ps -a --filter "volume=$Name" --format "{{.Names}}" | Where-Object { $_ })
}

function Remove-DockerVolumeIfPresent {
    param(
        [string]$Name,
        [string]$ServiceHint
    )

    $existing = docker volume ls --format "{{.Name}}" | Where-Object { $_ -eq $Name }
    if (-not $existing) {
        Write-Host "Volume not present, skipped: $Name" -ForegroundColor Yellow
        return
    }

    $users = Get-VolumeUsers -Name $Name
    if ($users.Count -gt 0) {
        throw "Volume is in use: $Name. Containers: $($users -join ', '). Stop $ServiceHint or run bootstrap with -ResetModelCaches before stack startup."
    }

    Write-Host "Removing Docker volume: $Name" -ForegroundColor Cyan
    docker volume rm $Name
    if ($LASTEXITCODE -ne 0) {
        throw "Docker failed to remove volume: $Name"
    }

    $stillExists = docker volume ls --format "{{.Name}}" | Where-Object { $_ -eq $Name }
    if ($stillExists) {
        throw "Volume still exists after removal attempt: $Name"
    }
}

Push-Location $repoRoot
try {
    $projectName = Get-ComposeProjectName
    $volumes = @(
        @{ Name = "${projectName}_ollama-data"; ServiceHint = "ollama" },
        @{ Name = "${projectName}_whisper-model-cache"; ServiceHint = "whisper-worker" }
    )

    Write-Host "This script deletes only model cache volumes:" -ForegroundColor Yellow
    foreach ($volume in $volumes) {
        Write-Host "- $($volume.Name)" -ForegroundColor Yellow
    }
    Write-Host ""
    Write-Host "It never deletes PostgreSQL, Redis, audio-storage, or vault files." -ForegroundColor Green

    if (-not $Force) {
        $confirmation = Read-Host "Type DELETE-MODEL-CACHES to continue"
        if ($confirmation -ne "DELETE-MODEL-CACHES") {
            Write-Host "Aborted. No volumes were deleted." -ForegroundColor Yellow
            exit 1
        }
    }

    foreach ($volume in $volumes) {
        Remove-DockerVolumeIfPresent -Name $volume.Name -ServiceHint $volume.ServiceHint
    }

    Write-Host "Model cache reset complete." -ForegroundColor Green
} catch {
    Write-Host ""
    Write-Host "Failed to reset model caches." -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    Write-Host "If a volume is in use, stop the stack first and retry." -ForegroundColor Yellow
    exit 1
} finally {
    Pop-Location
}
