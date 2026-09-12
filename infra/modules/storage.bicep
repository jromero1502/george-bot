@description('Globally-unique storage account name (lowercase, alphanumeric, <=24 chars)')
param storageAccountName string

param location string

param tags object = {}

var queueNames = [
  'george-incoming'
  'george-incoming-poison'
  'george-reminders'
  'george-reminders-poison'
]

resource storageAccount 'Microsoft.Storage/storageAccounts@2023-01-01' = {
  name: storageAccountName
  location: location
  tags: tags
  kind: 'StorageV2'
  sku: {
    name: 'Standard_LRS'
  }
  properties: {
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    allowBlobPublicAccess: false
    allowSharedKeyAccess: true
    accessTier: 'Hot'
    networkAcls: {
      defaultAction: 'Allow'
      bypass: 'AzureServices'
    }
  }
}

resource queueService 'Microsoft.Storage/storageAccounts/queueServices@2023-01-01' = {
  parent: storageAccount
  name: 'default'
}

resource queues 'Microsoft.Storage/storageAccounts/queueServices/queues@2023-01-01' = [
  for queueName in queueNames: {
    parent: queueService
    name: queueName
  }
]

// Diagnostic settings (see diagnostics.bicep) write logs as blobs into
// auto-created containers like insights-logs-functionapplogs. Expire them
// after 90 days so we don't accumulate storage cost indefinitely, since
// there is no Application Insights retention policy doing this for us.
resource lifecyclePolicy 'Microsoft.Storage/storageAccounts/managementPolicies@2023-01-01' = {
  parent: storageAccount
  name: 'default'
  properties: {
    policy: {
      rules: [
        {
          name: 'expire-diagnostic-logs'
          enabled: true
          type: 'Lifecycle'
          definition: {
            filters: {
              blobTypes: [
                'blockBlob'
              ]
              prefixMatch: [
                'insights-logs'
                'insights-metrics'
              ]
            }
            actions: {
              baseBlob: {
                delete: {
                  daysAfterModificationGreaterThan: 90
                }
              }
            }
          }
        }
      ]
    }
  }
}

output storageAccountId string = storageAccount.id
output storageAccountName string = storageAccount.name
#disable-next-line outputs-should-not-contain-secrets // consumed only as a @secure() param by the functionapp module, never surfaced to a user
output primaryConnectionString string = 'DefaultEndpointsProtocol=https;AccountName=${storageAccount.name};AccountKey=${storageAccount.listKeys().keys[0].value};EndpointSuffix=${environment().suffixes.storage}'
output queueNames array = queueNames
