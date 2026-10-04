@description('Document Intelligence (Cognitive Services, kind=FormRecognizer) account name')
param accountName string

param location string

param tags object = {}

@description('S0 (pay-per-page) or F0 (free, 500 pages/month + low rate limit)')
@allowed(['S0', 'F0'])
param skuName string = 'S0'

resource account 'Microsoft.CognitiveServices/accounts@2024-10-01' = {
  name: accountName
  location: location
  tags: tags
  kind: 'FormRecognizer'
  sku: {
    name: skuName
  }
  properties: {
    // Required for Entra ID (managed identity) auth — without a custom
    // subdomain, Cognitive Services only accepts key-based auth. Same
    // managed-identity-only posture as Cosmos (disableLocalAuth: true there).
    customSubDomainName: accountName
    publicNetworkAccess: 'Enabled'
    disableLocalAuth: true
  }
}

output endpoint string = account.properties.endpoint
output accountId string = account.id
output accountName string = account.name
