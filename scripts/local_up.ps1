<#
.SYNOPSIS
    Starts/checks the local dependencies needed to run George on localhost:
    Azurite (storage emulator, for queues) and the Cosmos DB Emulator.
#>
param(
    [string]$AzuriteDataDir = "$PSScriptRoot/../.azurite"
)

$ErrorActionPreference = "Stop"

New-Item -ItemType Directory -Force -Path $AzuriteDataDir | Out-Null

function Test-Port {
    param([string]$HostName, [int]$Port)
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $iar = $client.BeginConnect($HostName, $Port, $null, $null)
        $ok = $iar.AsyncWaitHandle.WaitOne(1000, $false)
        $client.Close()
        return $ok
    }
    catch {
        return $false
    }
}

Write-Host "==> Checking Cosmos DB Emulator (https://localhost:8081)" -ForegroundColor Cyan
if (Test-Port -HostName "localhost" -Port 8081) {
    Write-Host "    Already running." -ForegroundColor Green
}
else {
    Write-Host "    Not running. Start it, e.g.:" -ForegroundColor Yellow
    Write-Host '      & "$env:ProgramFiles\Azure Cosmos DB Emulator\Microsoft.Azure.Cosmos.Emulator.exe" /NoUI'
    Write-Host "    Not installed? winget install Microsoft.Azure.CosmosEmulator"
}

Write-Host "`n==> Checking Azurite queue service (http://localhost:10001)" -ForegroundColor Cyan
if (Test-Port -HostName "localhost" -Port 10001) {
    Write-Host "    Already running." -ForegroundColor Green
}
else {
    Write-Host "    Starting Azurite (blob + queue) in $AzuriteDataDir ..." -ForegroundColor Yellow
    try {
        Start-Process -FilePath "npx" `
            -ArgumentList "azurite", "--silent", "--location", $AzuriteDataDir, "--blobPort", "10000", "--queuePort", "10001" `
            -WindowStyle Minimized
    }
    catch {
        Write-Host "    Could not launch Azurite via npx. Install it globally: npm install -g azurite" -ForegroundColor Red
        throw
    }
    Start-Sleep -Seconds 2
    if (Test-Port -HostName "localhost" -Port 10001) {
        Write-Host "    Azurite is up." -ForegroundColor Green
    }
    else {
        Write-Host "    Could not confirm Azurite started yet — give it a few more seconds, or check for errors." -ForegroundColor Yellow
    }
}

Write-Host "`n==> Next steps:" -ForegroundColor Cyan
Write-Host "    1. Copy src/local.settings.json.example to src/local.settings.json and fill in ANTHROPIC_API_KEY / GROQ_API_KEY"
Write-Host "    2. python scripts/seed_cosmos.py --platform-admin-chat-id 111111111 ``"
Write-Host "         --demo-tenant-owner-chat-id 222222222"
Write-Host "    3. cd src; func start"
Write-Host "    4. python scripts/simulate_update.py --chat 222222222 --text 'cuanto me debe Maria'"
