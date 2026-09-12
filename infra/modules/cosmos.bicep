@description('Cosmos DB account name (globally unique, lowercase)')
param cosmosAccountName string

param location string

param databaseName string = 'george'

param tags object = {}

resource cosmosAccount 'Microsoft.DocumentDB/databaseAccounts@2024-08-15' = {
  name: cosmosAccountName
  location: location
  tags: tags
  kind: 'GlobalDocumentDB'
  properties: {
    databaseAccountOfferType: 'Standard'
    // Serverless: pay per request, no provisioned RU/s anywhere in this account.
    capabilities: [
      {
        name: 'EnableServerless'
      }
    ]
    consistencyPolicy: {
      defaultConsistencyLevel: 'Session'
    }
    locations: [
      {
        locationName: location
        failoverPriority: 0
        isZoneRedundant: false
      }
    ]
    backupPolicy: {
      type: 'Periodic'
      periodicModeProperties: {
        backupIntervalInMinutes: 240
        backupRetentionIntervalInHours: 168
        backupStorageRedundancy: 'Local'
      }
    }
    // Data-plane access is granted exclusively via the SQL role assignment
    // (see main.bicep) to the Function App's managed identity — no account keys.
    disableLocalAuth: true
    publicNetworkAccess: 'Enabled'
    minimalTlsVersion: 'Tls12'
  }
}

resource database 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases@2024-08-15' = {
  parent: cosmosAccount
  name: databaseName
  properties: {
    resource: {
      id: databaseName
    }
  }
}

var defaultIndexingPolicy = {
  indexingMode: 'consistent'
  automatic: true
  includedPaths: [
    {
      path: '/*'
    }
  ]
  excludedPaths: [
    {
      path: '/"_etag"/?'
    }
  ]
}

// Platform-level: one document per business. Small, admin-managed container.
resource tenantsContainer 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-08-15' = {
  parent: database
  name: 'tenants'
  properties: {
    resource: {
      id: 'tenants'
      partitionKey: {
        paths: [
          '/id'
        ]
        kind: 'Hash'
      }
      indexingPolicy: defaultIndexingPolicy
    }
  }
}

// A chat can belong to several tenants at once (chats.memberships[]), each
// with its own role — so there's no single scalar tenantId/role to build a
// composite index on. list_chats_by_role queries into the array with
// EXISTS(), which the default (every-path) indexing policy already covers;
// point reads are always by /chatId regardless of tenant.
resource chatsContainer 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-08-15' = {
  parent: database
  name: 'chats'
  properties: {
    resource: {
      id: 'chats'
      partitionKey: {
        paths: [
          '/chatId'
        ]
        kind: 'Hash'
      }
      indexingPolicy: defaultIndexingPolicy
    }
  }
}

resource conversationsContainer 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-08-15' = {
  parent: database
  name: 'conversations'
  properties: {
    resource: {
      id: 'conversations'
      partitionKey: {
        paths: [
          '/chatId'
        ]
        kind: 'Hash'
      }
      indexingPolicy: union(defaultIndexingPolicy, {
        compositeIndexes: [
          [
            {
              path: '/chatId'
              order: 'ascending'
            }
            {
              path: '/ts'
              order: 'descending'
            }
          ]
        ]
      })
    }
  }
}

// Partitioned by /tenantId (not /clientId): every access pattern — search,
// list, get-by-id — is scoped to "this tenant's clients", so partitioning on
// tenant makes isolation structural (a point read with the wrong tenantId as
// partition key simply can't find another tenant's document) instead of
// something every query has to remember to filter on.
resource clientsContainer 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-08-15' = {
  parent: database
  name: 'clients'
  properties: {
    resource: {
      id: 'clients'
      partitionKey: {
        paths: [
          '/tenantId'
        ]
        kind: 'Hash'
      }
      indexingPolicy: union(defaultIndexingPolicy, {
        compositeIndexes: [
          [
            {
              path: '/tenantId'
              order: 'ascending'
            }
            {
              path: '/name'
              order: 'ascending'
            }
          ]
        ]
      })
    }
  }
}

resource financeContainer 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-08-15' = {
  parent: database
  name: 'finance'
  properties: {
    resource: {
      id: 'finance'
      partitionKey: {
        paths: [
          '/tenantId'
        ]
        kind: 'Hash'
      }
      indexingPolicy: union(defaultIndexingPolicy, {
        compositeIndexes: [
          [
            {
              path: '/tenantId'
              order: 'ascending'
            }
            {
              path: '/clientId'
              order: 'ascending'
            }
            {
              path: '/createdAt'
              order: 'descending'
            }
          ]
          [
            {
              path: '/tenantId'
              order: 'ascending'
            }
            {
              path: '/period'
              order: 'descending'
            }
          ]
        ]
      })
    }
  }
}

// Stays keyed by /id (not /tenantId): the timer-triggered scheduler scans
// due reminders across every tenant in one query, so there's no single
// tenant partition to scope it to. Isolation for the per-tenant "list my
// reminders" tool happens via an explicit tenantId filter in the query
// instead (see repositories/reminders.py).
resource remindersContainer 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-08-15' = {
  parent: database
  name: 'reminders'
  properties: {
    resource: {
      id: 'reminders'
      partitionKey: {
        paths: [
          '/id'
        ]
        kind: 'Hash'
      }
      indexingPolicy: union(defaultIndexingPolicy, {
        compositeIndexes: [
          [
            {
              path: '/status'
              order: 'ascending'
            }
            {
              path: '/nextRunAt'
              order: 'ascending'
            }
          ]
          [
            {
              path: '/tenantId'
              order: 'ascending'
            }
            {
              path: '/nextRunAt'
              order: 'ascending'
            }
          ]
        ]
      })
    }
  }
}

resource pqrsContainer 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-08-15' = {
  parent: database
  name: 'pqrs'
  properties: {
    resource: {
      id: 'pqrs'
      partitionKey: {
        paths: [
          '/tenantId'
        ]
        kind: 'Hash'
      }
      indexingPolicy: union(defaultIndexingPolicy, {
        compositeIndexes: [
          [
            {
              path: '/tenantId'
              order: 'ascending'
            }
            {
              path: '/createdAt'
              order: 'descending'
            }
          ]
        ]
      })
    }
  }
}

// Platform-level, admin-managed settings — currently a single document (the
// default reminder templates create_tenant seeds onto every new business).
// Kept separate from `tenants` so that container stays "one document per
// real business" and list_tenants never needs to filter out a config doc.
resource platformConfigContainer 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-08-15' = {
  parent: database
  name: 'platformConfig'
  properties: {
    resource: {
      id: 'platformConfig'
      partitionKey: {
        paths: [
          '/id'
        ]
        kind: 'Hash'
      }
      indexingPolicy: defaultIndexingPolicy
    }
  }
}

output accountName string = cosmosAccount.name
output accountId string = cosmosAccount.id
output endpoint string = cosmosAccount.properties.documentEndpoint
output databaseName string = database.name
