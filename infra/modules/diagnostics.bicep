@description('Name of the storage account that receives diagnostic logs (and that itself is the storage account whose queue metrics are logged)')
param storageAccountName string

param functionAppName string

param cosmosAccountName string

resource storageAccount 'Microsoft.Storage/storageAccounts@2023-01-01' existing = {
  name: storageAccountName
}

resource queueService 'Microsoft.Storage/storageAccounts/queueServices@2023-01-01' existing = {
  parent: storageAccount
  name: 'default'
}

resource functionApp 'Microsoft.Web/sites@2023-12-01' existing = {
  name: functionAppName
}

resource cosmosAccount 'Microsoft.DocumentDB/databaseAccounts@2024-08-15' existing = {
  name: cosmosAccountName
}

// No Application Insights in this deployment — diagnostic settings land
// everything as JSON blobs in the same storage account instead
// (containers auto-created: insights-logs-<category>, insights-metrics-<category>).
resource diagFunctionApp 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'diag-to-storage'
  scope: functionApp
  properties: {
    storageAccountId: storageAccount.id
    logs: [
      {
        category: 'FunctionAppLogs'
        enabled: true
      }
    ]
    metrics: [
      {
        category: 'AllMetrics'
        enabled: true
      }
    ]
  }
}

resource diagCosmos 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'diag-to-storage'
  scope: cosmosAccount
  properties: {
    storageAccountId: storageAccount.id
    logs: [
      {
        category: 'DataPlaneRequests'
        enabled: true
      }
      {
        category: 'QueryRuntimeStatistics'
        enabled: true
      }
      {
        category: 'ControlPlaneRequests'
        enabled: true
      }
    ]
    metrics: [
      {
        category: 'Requests'
        enabled: true
      }
    ]
  }
}

resource diagQueues 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'diag-to-storage'
  scope: queueService
  properties: {
    storageAccountId: storageAccount.id
    logs: [
      {
        category: 'StorageRead'
        enabled: true
      }
      {
        category: 'StorageWrite'
        enabled: true
      }
      {
        category: 'StorageDelete'
        enabled: true
      }
    ]
    metrics: [
      {
        category: 'Transaction'
        enabled: true
      }
    ]
  }
}
