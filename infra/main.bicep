targetScope = 'subscription'

@description('Name of the resource group to create (or reuse) for all of George\'s resources')
param resourceGroupName string

@description('Short name used to derive all resource names (lowercase letters/numbers only)')
@minLength(3)
@maxLength(11)
param appName string = 'george'

@description('Deployment environment tag, also folded into resource names')
param environmentName string = 'prod'

param location string = 'eastus2'

@secure()
param telegramBotToken string

@secure()
param telegramWebhookSecret string

@description('Random path segment for the webhook URL, e.g. a UUID — obscurity layer on top of the secret token. Not marked @secure(): it is not the security boundary (the X-Telegram-Bot-Api-Secret-Token header is) and needs to be readable back as a deployment output to build the webhook URL.')
param telegramWebhookPath string

@secure()
param anthropicApiKey string

@secure()
param groqApiKey string

@description('Telegram chatId of the platform operator — seeded with role=platform_admin, receives poison-queue alerts, and is the only role that can create new tenants (businesses) via the create_tenant tool')
param platformAdminChatId string

param anthropicModel string = 'claude-haiku-4-5'

param defaultTimezone string = 'America/Bogota'

param historyTurns int = 12

param dryRun bool = false

@description('Object id of an extra principal (e.g. CI/CD service principal) to grant Cosmos DB data-plane RBAC, on top of the Function App\'s own managed identity. See resources.bicep for why.')
param deployerPrincipalId string = ''

// Subscription-scope deployment creates the resource group itself, so a
// single `az deployment sub create` is the whole deployment — no separate
// `az group create` step needed beforehand.
resource rg 'Microsoft.Resources/resourceGroups@2024-11-01' = {
  name: resourceGroupName
  location: location
  tags: {
    app: appName
    environment: environmentName
    'managed-by': 'bicep'
  }
}

module resources 'modules/resources.bicep' = {
  name: 'resources'
  scope: rg
  params: {
    appName: appName
    environmentName: environmentName
    location: location
    telegramBotToken: telegramBotToken
    telegramWebhookSecret: telegramWebhookSecret
    telegramWebhookPath: telegramWebhookPath
    anthropicApiKey: anthropicApiKey
    groqApiKey: groqApiKey
    platformAdminChatId: platformAdminChatId
    anthropicModel: anthropicModel
    defaultTimezone: defaultTimezone
    historyTurns: historyTurns
    dryRun: dryRun
    deployerPrincipalId: deployerPrincipalId
  }
}

output resourceGroupName string = rg.name
output functionAppName string = resources.outputs.functionAppName
output functionAppHostName string = resources.outputs.functionAppHostName
output cosmosEndpoint string = resources.outputs.cosmosEndpoint
output cosmosDatabaseName string = resources.outputs.cosmosDatabaseName
output storageAccountName string = resources.outputs.storageAccountName
output telegramWebhookUrl string = resources.outputs.telegramWebhookUrl
