#!/usr/bin/env python
"""Idempotently creates the Cosmos DB database + containers (mirroring
infra/modules/cosmos.bicep), seeds the platform_admin chat — the only
account that can create tenants (businesses) once George is running, via the
create_tenant tool — and, the first time it runs, seeds platformConfig's
default reminder templates (the ones create_tenant sows into every new
tenant; platform_admin can later change them with set_default_reminder_templates).

Run against the Cosmos DB Emulator for local development:

    python scripts/seed_cosmos.py --platform-admin-chat-id 111111111

Or against a real, freshly-deployed account (needs an identity with Cosmos
DB Built-in Data Contributor on it — e.g. `az login` as yourself, since the
Function App's own managed identity isn't something you can run a script as):

    python scripts/seed_cosmos.py --endpoint https://<account>.documents.azure.com:443/ \\
        --platform-admin-chat-id 111111111 --platform-admin-name "Julian"

For local testing, --demo-tenant-owner-chat-id also seeds one example
tenant (business) with that chat as its owner, plus the platform's default
reminders — the same thing the create_tenant tool would do, so you don't
have to drive it through a chat conversation just to get test data:

    python scripts/seed_cosmos.py --platform-admin-chat-id 111111111 \\
        --demo-tenant-owner-chat-id 222222222 --demo-tenant-owner-name "Maria"
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC_DIR))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--endpoint", default=os.environ.get("COSMOS_ENDPOINT", "https://localhost:8081"))
    parser.add_argument("--key", default=os.environ.get("COSMOS_KEY"), help="Omit against a real Azure account to use az login (DefaultAzureCredential)")
    parser.add_argument("--database", default=os.environ.get("COSMOS_DATABASE", "george"))
    parser.add_argument("--platform-admin-chat-id", required=True)
    parser.add_argument("--platform-admin-name", default="Admin")
    parser.add_argument(
        "--demo-tenant-owner-chat-id",
        default=None,
        help="If set, also seeds one example tenant (business) with this chat as owner, for local testing",
    )
    parser.add_argument("--demo-tenant-owner-name", default="Owner")
    parser.add_argument("--demo-tenant-name", default="George Demo")
    parser.add_argument("--demo-tenant-business-type", default="Paseo de perros")
    parser.add_argument("--demo-tenant-timezone", default="America/Bogota")
    parser.add_argument(
        "--second-tenant-role-for-owner",
        default=None,
        choices=["owner", "admin", "walker", "viewer"],
        help=(
            "If set (needs --demo-tenant-owner-chat-id too), also creates a SECOND demo tenant and adds that "
            "same chat to it with this role — for testing a chat that belongs to more than one business."
        ),
    )
    parser.add_argument("--second-tenant-name", default="Pelos y Colas")
    parser.add_argument("--second-tenant-business-type", default="Peluqueria canina")
    parser.add_argument("--demo-tenant-currency", default="COP")
    args = parser.parse_args()

    # george.config.settings is a module-level singleton read at import time —
    # populate the environment *before* importing anything under george.*.
    os.environ["COSMOS_ENDPOINT"] = args.endpoint
    os.environ["COSMOS_DATABASE"] = args.database
    if args.key:
        os.environ["COSMOS_KEY"] = args.key
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "seed-script-placeholder")
    os.environ.setdefault("TELEGRAM_WEBHOOK_SECRET", "seed-script-placeholder")
    os.environ.setdefault("TELEGRAM_WEBHOOK_PATH", "seed-script-placeholder")
    os.environ.setdefault("ANTHROPIC_API_KEY", "seed-script-placeholder")
    os.environ.setdefault("GROQ_API_KEY", "seed-script-placeholder")
    os.environ["PLATFORM_ADMIN_CHAT_ID"] = args.platform_admin_chat_id

    from azure.cosmos import CosmosClient, PartitionKey, exceptions
    from azure.identity import DefaultAzureCredential

    from george import scheduling
    from george.repositories import chats as chats_repo
    from george.repositories import platform_config as platform_config_repo
    from george.repositories import reminders as reminders_repo
    from george.repositories import tenants as tenants_repo

    credential = args.key or DefaultAzureCredential()
    verify_tls = args.key is None  # the emulator's cert is self-signed
    client = CosmosClient(args.endpoint, credential=credential, connection_verify=verify_tls)

    print(f"==> Ensuring database '{args.database}' exists at {args.endpoint}")
    database = client.create_database_if_not_exists(id=args.database)

    containers = {
        "tenants": "/id",
        "chats": "/chatId",
        "conversations": "/chatId",
        "clients": "/tenantId",
        "finance": "/tenantId",
        "reminders": "/id",
        "pqrs": "/tenantId",
        "platformConfig": "/id",
    }
    for name, pk_path in containers.items():
        database.create_container_if_not_exists(id=name, partition_key=PartitionKey(path=pk_path))
        print(f"  container ready: {name} (pk {pk_path})")

    print("==> Ensuring default reminder templates exist in platformConfig")
    try:
        platform_config_repo._container().read_item(
            item="default_reminders", partition_key="default_reminders"
        )
        print("  default_reminders already configured, leaving as-is")
    except exceptions.CosmosResourceNotFoundError:
        platform_config_repo.set_default_reminder_templates(
            platform_config_repo.FALLBACK_DEFAULT_REMINDER_TEMPLATES, updated_by=args.platform_admin_chat_id
        )
        print("  seeded default_reminders")

    print(f"==> Seeding platform_admin chat {args.platform_admin_chat_id}")
    chats_repo.set_platform_admin(
        args.platform_admin_chat_id,
        name=args.platform_admin_name,
        locale="es-CO",
    )

    if args.demo_tenant_owner_chat_id:
        print(f"==> Seeding demo tenant '{args.demo_tenant_name}'")
        tenant = tenants_repo.create_tenant(
            name=args.demo_tenant_name,
            business_type=args.demo_tenant_business_type,
            created_by=args.platform_admin_chat_id,
            description=f"Negocio de demostracion: {args.demo_tenant_business_type.lower()}.",
            currency=args.demo_tenant_currency,
            timezone=args.demo_tenant_timezone,
        )
        print(f"  tenant ready: {tenant['id']}")

        chats_repo.add_membership(
            args.demo_tenant_owner_chat_id,
            tenant["id"],
            "owner",
            name=args.demo_tenant_owner_name,
            timezone=args.demo_tenant_timezone,
            locale="es-CO",
        )
        print(f"  owner chat ready: {args.demo_tenant_owner_chat_id}")

        for reminder in platform_config_repo.get_default_reminder_templates():
            schedule = {**reminder["schedule"], "timezone": args.demo_tenant_timezone}
            next_run_at = scheduling.compute_next_run(schedule)
            reminders_repo.upsert_reminder(
                tenant["id"],
                None,
                args.platform_admin_chat_id,
                name=reminder["name"],
                kind=reminder["kind"],
                body=reminder["body"],
                schedule=schedule,
                target=reminder["target"],
                status="active",
                system=True,
                nextRunAt=next_run_at,
            )
            print(f"  reminder ready: {reminder['name']} -> next run {next_run_at}")

        if args.second_tenant_role_for_owner:
            print(f"==> Seeding second demo tenant '{args.second_tenant_name}' (multi-business test)")
            second_tenant = tenants_repo.create_tenant(
                name=args.second_tenant_name,
                business_type=args.second_tenant_business_type,
                created_by=args.platform_admin_chat_id,
                description=f"Negocio de demostracion: {args.second_tenant_business_type.lower()}.",
                currency=args.demo_tenant_currency,
                timezone=args.demo_tenant_timezone,
            )
            print(f"  tenant ready: {second_tenant['id']}")

            # set_active=False: the first membership set the chat's
            # activeTenantId already — adding a second one shouldn't silently
            # move it, that's what the pending-selection flow / switch_business
            # are for.
            chats_repo.add_membership(
                args.demo_tenant_owner_chat_id,
                second_tenant["id"],
                args.second_tenant_role_for_owner,
                set_active=False,
            )
            print(
                f"  chat {args.demo_tenant_owner_chat_id} added as "
                f"{args.second_tenant_role_for_owner!r} of {second_tenant['id']} too"
            )

            for reminder in platform_config_repo.get_default_reminder_templates():
                schedule = {**reminder["schedule"], "timezone": args.demo_tenant_timezone}
                next_run_at = scheduling.compute_next_run(schedule)
                reminders_repo.upsert_reminder(
                    second_tenant["id"],
                    None,
                    args.platform_admin_chat_id,
                    name=reminder["name"],
                    kind=reminder["kind"],
                    body=reminder["body"],
                    schedule=schedule,
                    target=reminder["target"],
                    status="active",
                    system=True,
                    nextRunAt=next_run_at,
                )

    print("\nDone.")


if __name__ == "__main__":
    main()
