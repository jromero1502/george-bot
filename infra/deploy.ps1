<#
.SYNOPSIS
    Deploys George's Azure infrastructure (including the resource group
    itself) and publishes the Function App code.

.DESCRIPTION
    1. Runs `az deployment sub create` against main.bicep, a
       subscription-scope template that creates the resource group and
       everything inside it in one deployment.
    2. Publishes ../src via `func azure functionapp publish`.

.EXAMPLE
    ./deploy.ps1 -ResourceGroup rg-george-bot -ParametersFile ./main.parameters.json
#>
param(
    [Parameter(Mandatory = $true)]
    [string]$ResourceGroup,

    [string]$Location = "eastus2",

    [string]$ParametersFile = "$PSScriptRoot/main.parameters.json",

    [switch]$SkipPublish
)

$ErrorActionPreference = "Stop"

# Freshly-installed CLI tools (func, azurite) land in a PATH entry that an
# already-open shell won't pick up until it's refreshed explicitly.
$env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")

Write-Host "==> Deploying infra/main.bicep (creates resource group '$ResourceGroup' in $Location, then everything inside it)" -ForegroundColor Cyan
# Subscription-scope deployment names are unique per subscription and pin to
# the location first used with that name — derive one from the resource
# group so redeploying to a different RG/region never collides with a prior run.
$deploymentName = "deploy-$ResourceGroup"
$deployment = az deployment sub create `
    --name $deploymentName `
    --location $Location `
    --template-file "$PSScriptRoot/main.bicep" `
    --parameters "@$ParametersFile" `
    --parameters resourceGroupName=$ResourceGroup location=$Location `
    --query "properties.outputs" `
    --output json | ConvertFrom-Json

$functionAppName = $deployment.functionAppName.value
$webhookUrl = $deployment.telegramWebhookUrl.value

Write-Host "==> Deployed Function App: $functionAppName" -ForegroundColor Green
Write-Host "==> Webhook URL (needs a function key appended as ?code=...): $webhookUrl" -ForegroundColor Green

if (-not $SkipPublish) {
    Write-Host "==> Publishing src/ to $functionAppName" -ForegroundColor Cyan
    Push-Location "$PSScriptRoot/../src"
    try {
        func azure functionapp publish $functionAppName --python
    }
    finally {
        Pop-Location
    }
}
else {
    Write-Host "==> Skipped publish (-SkipPublish). Run manually:" -ForegroundColor Yellow
    Write-Host "    cd src; func azure functionapp publish $functionAppName --python"
}

Write-Host ""
Write-Host "Next steps:" -ForegroundColor Cyan
Write-Host "  1. Get a function key:  az functionapp keys list -g $ResourceGroup -n $functionAppName --query 'functionKeys.default' -o tsv"
Write-Host "  2. Register the webhook: ./scripts/set_webhook.ps1 -FunctionAppUrl '$webhookUrl' -FunctionKey '<key>' -Secret '<TELEGRAM_WEBHOOK_SECRET value>' -BotToken '<bot token>'"
Write-Host "  3. Seed Cosmos DB:       python scripts/seed_cosmos.py --endpoint <cosmosEndpoint> --platform-admin-chat-id <platformAdminChatId>  (run with an identity that has Cosmos data-plane RBAC, e.g. your own az login)"
