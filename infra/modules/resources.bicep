// Deployed as a module scoped to the resource group main.bicep creates
// (subscription-scope deployment) — see ../main.bicep.

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

@description('Object id of an extra principal (e.g. the CI/CD service principal running seed_cosmos.py) to grant Cosmos DB data-plane RBAC. The account has disableLocalAuth: true, so anything that needs to read/write data outside the Function App\'s own managed identity needs this — an az login user gets it via the CLAUDE.md instructions, automation gets it here. Empty string (default) skips the extra assignment.')
param deployerPrincipalId string = ''

var tags = {
  app: appName
  environment: environmentName
  'managed-by': 'bicep'
}

// Storage account names must be globally unique, <=24 chars, lowercase alphanumeric only.
var uniqueSuffix = uniqueString(resourceGroup().id)
var storageAccountName = toLower('st${appName}${uniqueSuffix}')
var cosmosAccountName = toLower('cosmos-${appName}-${environmentName}-${uniqueSuffix}')
var functionAppName = toLower('func-${appName}-${environmentName}-${uniqueSuffix}')
var appServicePlanName = toLower('plan-${appName}-${environmentName}')
var databaseName = 'george'

module storage 'storage.bicep' = {
  name: 'storage'
  params: {
    storageAccountName: storageAccountName
    location: location
    tags: tags
  }
}

module cosmos 'cosmos.bicep' = {
  name: 'cosmos'
  params: {
    cosmosAccountName: cosmosAccountName
    location: location
    databaseName: databaseName
    tags: tags
  }
}

module functionApp 'functionapp.bicep' = {
  name: 'functionapp'
  params: {
    functionAppName: functionAppName
    appServicePlanName: appServicePlanName
    location: location
    tags: tags
    storageConnectionString: storage.outputs.primaryConnectionString
    cosmosEndpoint: cosmos.outputs.endpoint
    cosmosDatabaseName: cosmos.outputs.databaseName
    telegramBotToken: telegramBotToken
    telegramWebhookSecret: telegramWebhookSecret
    telegramWebhookPath: telegramWebhookPath
    anthropicApiKey: anthropicApiKey
    groqApiKey: groqApiKey
    anthropicModel: anthropicModel
    defaultTimezone: defaultTimezone
    platformAdminChatId: platformAdminChatId
    historyTurns: historyTurns
    dryRun: dryRun
  }
}

// Grants the Function App's system-assigned identity data-plane access to
// Cosmos DB (the account has disableLocalAuth: true — this role assignment
// is the *only* way in, no keys anywhere).
resource cosmosAccountRef 'Microsoft.DocumentDB/databaseAccounts@2024-08-15' existing = {
  name: cosmosAccountName
  dependsOn: [
    cosmos
  ]
}

var cosmosDataContributorRoleId = '00000000-0000-0000-0000-000000000002'

resource cosmosDataAccess 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-08-15' = {
  parent: cosmosAccountRef
  name: guid(cosmosAccountRef.id, functionAppName, 'data-contributor')
  properties: {
    roleDefinitionId: '${cosmosAccountRef.id}/sqlRoleDefinitions/${cosmosDataContributorRoleId}'
    principalId: functionApp.outputs.principalId
    scope: cosmosAccountRef.id
  }
}

// Same grant for an external automation principal (CI/CD), so scripts like
// seed_cosmos.py can run unattended without az-login-as-a-human. Skipped
// when deployerPrincipalId is left empty (local/manual deploys).
resource cosmosDataAccessDeployer 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-08-15' = if (!empty(deployerPrincipalId)) {
  parent: cosmosAccountRef
  name: guid(cosmosAccountRef.id, deployerPrincipalId, 'deployer')
  properties: {
    roleDefinitionId: '${cosmosAccountRef.id}/sqlRoleDefinitions/${cosmosDataContributorRoleId}'
    principalId: deployerPrincipalId
    scope: cosmosAccountRef.id
  }
}

module diagnostics 'diagnostics.bicep' = {
  name: 'diagnostics'
  params: {
    storageAccountName: storage.outputs.storageAccountName
    functionAppName: functionApp.outputs.functionAppName
    cosmosAccountName: cosmos.outputs.accountName
  }
}

output functionAppName string = functionApp.outputs.functionAppName
output functionAppHostName string = functionApp.outputs.defaultHostName
output cosmosEndpoint string = cosmos.outputs.endpoint
output cosmosDatabaseName string = cosmos.outputs.databaseName
output storageAccountName string = storage.outputs.storageAccountName
output telegramWebhookUrl string = 'https://${functionApp.outputs.defaultHostName}/api/telegram/${telegramWebhookPath}'
