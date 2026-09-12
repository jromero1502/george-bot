@description('Function App name (becomes <name>.azurewebsites.net)')
param functionAppName string

param appServicePlanName string

param location string

param tags object = {}

@description('Full storage connection string, used for both the Functions host and the queue triggers')
@secure()
param storageConnectionString string

param cosmosEndpoint string

param cosmosDatabaseName string

@secure()
param telegramBotToken string

@secure()
param telegramWebhookSecret string

@description('Not @secure() — see main.bicep for why; it is a URL path segment, not the auth boundary')
param telegramWebhookPath string

@secure()
param anthropicApiKey string

@secure()
param groqApiKey string

param anthropicModel string = 'claude-haiku-4-5'

param defaultTimezone string = 'America/Bogota'

param platformAdminChatId string

param historyTurns int = 12

param dryRun bool = false

// Consumption (Y1) plan — the only SKU that supports functionAppScaleLimit
// down to 1 alongside scale-to-zero. Flex Consumption's minimum
// maximumInstanceCount is 40, which can't satisfy "max 1 instance".
resource appServicePlan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: appServicePlanName
  location: location
  tags: tags
  sku: {
    name: 'Y1'
    tier: 'Dynamic'
  }
  kind: 'functionapp'
  properties: {
    reserved: true // required for Linux plans
  }
}

resource functionApp 'Microsoft.Web/sites@2023-12-01' = {
  name: functionAppName
  location: location
  tags: tags
  kind: 'functionapp,linux'
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    serverFarmId: appServicePlan.id
    httpsOnly: true
    siteConfig: {
      linuxFxVersion: 'Python|3.11'
      minTlsVersion: '1.2'
      ftpsState: 'Disabled'
      // Consumption plan hard cap — this is the "max instances 1" requirement.
      functionAppScaleLimit: 1
      appSettings: [
        {
          name: 'AzureWebJobsStorage'
          value: storageConnectionString
        }
        {
          name: 'WEBSITE_CONTENTAZUREFILECONNECTIONSTRING'
          value: storageConnectionString
        }
        {
          name: 'WEBSITE_CONTENTSHARE'
          value: toLower(functionAppName)
        }
        {
          name: 'FUNCTIONS_EXTENSION_VERSION'
          value: '~4'
        }
        {
          name: 'FUNCTIONS_WORKER_RUNTIME'
          value: 'python'
        }
        {
          name: 'WEBSITE_MAX_DYNAMIC_APPLICATION_SCALE_OUT'
          value: '1'
        }
        {
          name: 'COSMOS_ENDPOINT'
          value: cosmosEndpoint
        }
        {
          name: 'COSMOS_DATABASE'
          value: cosmosDatabaseName
        }
        {
          name: 'TELEGRAM_BOT_TOKEN'
          value: telegramBotToken
        }
        {
          name: 'TELEGRAM_WEBHOOK_SECRET'
          value: telegramWebhookSecret
        }
        {
          name: 'TELEGRAM_WEBHOOK_PATH'
          value: telegramWebhookPath
        }
        {
          name: 'ANTHROPIC_API_KEY'
          value: anthropicApiKey
        }
        {
          name: 'GROQ_API_KEY'
          value: groqApiKey
        }
        {
          name: 'ANTHROPIC_MODEL'
          value: anthropicModel
        }
        {
          name: 'DEFAULT_TIMEZONE'
          value: defaultTimezone
        }
        {
          name: 'PLATFORM_ADMIN_CHAT_ID'
          value: platformAdminChatId
        }
        {
          name: 'HISTORY_TURNS'
          value: string(historyTurns)
        }
        {
          name: 'DRY_RUN'
          value: string(dryRun)
        }
      ]
      // Deliberately no APPLICATIONINSIGHTS_CONNECTION_STRING /
      // APPINSIGHTS_INSTRUMENTATIONKEY — observability goes through
      // diagnostic settings to storage instead (see diagnostics.bicep).
    }
  }
}

output functionAppName string = functionApp.name
output functionAppId string = functionApp.id
output principalId string = functionApp.identity.principalId
output defaultHostName string = functionApp.properties.defaultHostName
