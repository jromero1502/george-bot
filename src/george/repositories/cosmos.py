"""Cosmos DB client factory.

Auth strategy: if COSMOS_KEY is set (local dev against the Cosmos DB
Emulator), authenticate with the key and skip TLS verification (the
emulator's cert is self-signed). In Azure, COSMOS_KEY is never set — the
Function App's system-assigned managed identity is used instead, granted
"Cosmos DB Built-in Data Contributor" by infra/main.bicep. No account keys
ever touch the deployed app.
"""
from __future__ import annotations

import functools

from azure.cosmos import ContainerProxy, CosmosClient, DatabaseProxy
from azure.identity import DefaultAzureCredential

from george.config import settings

CONTAINER_NAMES = (
    "tenants",
    "chats",
    "conversations",
    "clients",
    "finance",
    "reminders",
    "pqrs",
    "platformConfig",
)


@functools.lru_cache(maxsize=1)
def get_client() -> CosmosClient:
    if settings.cosmos_key:
        return CosmosClient(
            settings.cosmos_endpoint,
            credential=settings.cosmos_key,
            connection_verify=False,  # emulator's self-signed cert
        )
    return CosmosClient(settings.cosmos_endpoint, credential=DefaultAzureCredential())


@functools.lru_cache(maxsize=1)
def get_database() -> DatabaseProxy:
    return get_client().get_database_client(settings.cosmos_database)


@functools.lru_cache(maxsize=None)
def get_container(name: str) -> ContainerProxy:
    if name not in CONTAINER_NAMES:
        raise ValueError(f"Unknown container: {name!r}. Expected one of {CONTAINER_NAMES}")
    return get_database().get_container_client(name)
