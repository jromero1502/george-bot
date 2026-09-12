<#
.SYNOPSIS
    Registers George's Telegram webhook, with both auth layers set: the
    function key (?code=) and the X-Telegram-Bot-Api-Secret-Token secret.

.EXAMPLE
    ./scripts/set_webhook.ps1 `
        -BotToken "123456:AAglibberish" `
        -FunctionAppUrl "https://func-george-prod-abcd12.azurewebsites.net/api/telegram/<TELEGRAM_WEBHOOK_PATH value>" `
        -FunctionKey "<function key from `az functionapp keys list`>" `
        -Secret "<TELEGRAM_WEBHOOK_SECRET value>"
#>
param(
    [Parameter(Mandatory = $true)][string]$BotToken,
    [Parameter(Mandatory = $true)][string]$FunctionAppUrl,
    [Parameter(Mandatory = $true)][string]$FunctionKey,
    [Parameter(Mandatory = $true)][string]$Secret
)

$ErrorActionPreference = "Stop"

$webhookUrl = "$FunctionAppUrl`?code=$FunctionKey"

$body = @{
    url             = $webhookUrl
    secret_token    = $Secret
    allowed_updates = @("message", "edited_message")
} | ConvertTo-Json

Write-Host "==> Registering webhook: $webhookUrl" -ForegroundColor Cyan
$response = Invoke-RestMethod -Method Post `
    -Uri "https://api.telegram.org/bot$BotToken/setWebhook" `
    -ContentType "application/json" `
    -Body $body

$response | ConvertTo-Json -Depth 5

Write-Host "`n==> Current webhook info:" -ForegroundColor Cyan
Invoke-RestMethod -Uri "https://api.telegram.org/bot$BotToken/getWebhookInfo" | ConvertTo-Json -Depth 5
