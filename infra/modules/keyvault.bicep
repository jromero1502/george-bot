@description('Key Vault name (3-24 chars, globally unique)')
param vaultName string

param location string

param tags object = {}

// RBAC authorization (no access policies) + soft delete/purge protection: these
// hold per-tenant Google OAuth credentials (client id/secret/refresh token),
// which are written by hand — losing one to an accidental delete means the
// owner has to redo the whole OAuth consent flow.
resource vault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: vaultName
  location: location
  tags: tags
  properties: {
    tenantId: subscription().tenantId
    sku: {
      family: 'A'
      name: 'standard'
    }
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 30
    enablePurgeProtection: true
    publicNetworkAccess: 'Enabled'
  }
}

output vaultName string = vault.name
output vaultUri string = vault.properties.vaultUri
