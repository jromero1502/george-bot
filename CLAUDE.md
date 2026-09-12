# CLAUDE.md

Guía de contexto para trabajar en este repo. Ver `README.md` para instrucciones de uso;
esto es sobre todo *por qué* está construido así.

## Qué es esto

Bot de Telegram ("George") **multi-tenant**: un mismo despliegue gestiona varios negocios de
servicios independientes y aislados entre sí (paseo de perros, peluquería, plomería, lo que
sea — el esquema de negocio es genérico, ver más abajo). Azure Functions (Python v2,
Consumption Y1) + Cosmos DB serverless + Claude Haiku (tool use) + Groq Whisper (STT).

**Nació como un bot de un solo rubro** (paseo de perros en Bogotá) y se generalizó a
multi-tenant/multi-rubro después — si encontrás texto o nombres que todavía asumen un solo
negocio, es resto de esa primera versión, repórtalo.

## Estructura

```
infra/            main.bicep (subscription scope, crea el resource group) -> modules/resources.bicep
                  (resource-group scope: storage, cosmos, functionapp, diagnostics) + deploy.ps1
src/
  function_app.py     Todos los triggers (Python v2 programming model)
  george/
    config.py          Settings desde env vars — instanciado una vez al importar
    roles.py            Constantes de rol compartidas (PLATFORM_ADMIN_ROLE, PENDING_SELECTION_ROLE, BUSINESS_ROLES)
    agent.py           Loop manual de tool use con Haiku (no el tool_runner beta del SDK)
    prompts.py         System prompt de George — por tenant, platform_admin, o pending_business_selection
    scheduling.py       cron/once -> nextRunAt en UTC (croniter + zoneinfo)
    telegram.py         Cliente Telegram Bot API (respeta DRY_RUN)
    groq_stt.py          Transcripción de voz
    tools/               Un módulo por dominio + registry.py que los agrega + tenants.py (platform_admin)
                         + membership.py (list_my_businesses/switch_business — gestión de la propia membership)
    repositories/        Un módulo por contenedor de Cosmos + cosmos.py (factory de cliente) + tenants.py
scripts/          seed_cosmos.py, simulate_update.py, set_webhook.ps1, local_up.ps1
tests/            pytest — scheduling y gating de tools/tenant, sin dependencias externas
```

## Modelo multi-tenant

- **Un chat puede pertenecer a varios tenants (negocios) a la vez**, cada uno con su propio rol
  — `chats.memberships: [{tenantId, role}, ...]`. `chats.activeTenantId` dice cuál de esas
  membresías está operando la conversación en este momento. Ver `repositories/chats.py`.
- **`isPlatformAdmin` es un flag aparte, no una membership.** Un chat con
  `isPlatformAdmin: true` no pertenece a ningún negocio y opera siempre en modo plataforma
  (rol efectivo `platform_admin`) — el modelo asume que un chat es *o* platform_admin *o*
  tiene negocios, no ambos a la vez (más simple, y es lo que pidió el usuario; nada en el
  esquema lo impide técnicamente si hiciera falta cambiarlo).
- **Solo el `platform_admin` puede crear la primera asociación chat↔tenant** — vía
  `create_tenant` (crea el negocio + membership `owner` para el chat que se indique) o
  `add_chat_to_tenant` (asocia un chat ya existente, o nuevo, a un tenant ya creado, con
  cualquier rol). No hay autoservicio: `telegram_webhook` descarta cualquier update de un
  `chatId` que no esté ya en la whitelist `chats`, así que un chat nunca se puede dar de alta
  solo. **Una vez que un chat ya pertenece a un tenant, el `owner` de ESE tenant puede seguir
  sumando/editando su propio equipo con `upsert_chat`** (restringido a `ctx.tenant_id`, nunca
  toca membership de otro negocio).
- **Resolución de rol/tenant activo por mensaje — `function_app._resolve_role_and_tenant`**:
  - `isPlatformAdmin` → rol `platform_admin`, sin tenant — **salvo que ese mismo chat también
    tenga membership de negocio propio y haya hecho `switch_business` hacia ella** (dual role,
    ver abajo), en cuyo caso opera en modo negocio hasta que pida volver.
  - Sin memberships → se rechaza el mensaje, "no estás asociado a ningún negocio".
  - Una sola membership → se autoselecciona como activa (y se persiste en
    `activeTenantId`) sin fricción — el caso común de un chat en un solo negocio no nota
    ninguna diferencia con la versión anterior de este bot. Esta autoselección **no aplica** a
    un chat `isPlatformAdmin` (ver abajo).
  - Varias memberships y ninguna activa (`activeTenantId` no coincide con ninguna, o es null)
    → rol transitorio `PENDING_SELECTION_ROLE`: el prompt de ese turno (`prompts.py`) le pide
    a George que llame `list_my_businesses`, le pregunte al usuario cuál quiere usar, y llame
    `switch_business(tenant_id)` para fijarlo — a partir de ahí opera normal hasta que el
    usuario pida cambiar de negocio de nuevo.
- **Un chat puede ser `platform_admin` Y owner/admin/etc. de un negocio propio ("dual role")**.
  `create_tenant`/`add_chat_to_tenant` ya no bloquean que el chat destino sea platform_admin.
  El **default de un chat así es siempre modo plataforma** — a diferencia del caso normal, agregar
  una membership a un chat platform_admin (`repositories/chats.py::add_membership`) **nunca**
  autoactiva esa membership aunque sea la primera (guard explícito: `not
  chat.get("isPlatformAdmin")` en la condición de autoactivación); solo se activa con
  `set_active=True` explícito o con un `switch_business` posterior. `list_my_businesses` y
  `switch_business` están abiertas también al rol `platform_admin` para que pueda ver/entrar a
  sus propios negocios; `switch_to_platform_admin` (tools/membership.py) es el camino de vuelta —
  se gatea con `ctx.is_platform_admin` (un campo de `ToolContext` que refleja el chat subyacente,
  no el modo activo de este turno) en vez de `ctx.role`, porque mientras opera un negocio propio
  `ctx.role` es un rol de negocio normal (`owner`, etc.), no `platform_admin`.
- **Aislamiento estructural, no solo un filtro de query**: `clients`, `finance` y `pqrs` están
  particionados por `/tenantId` en Cosmos DB. Un `read_item` con el `tenantId` equivocado como
  partition key no encuentra el documento aunque adivines el id exacto — no depende de que cada
  repo function recuerde agregar `WHERE tenantId = ...`. `reminders` es la excepción: sigue
  particionado por `/id` porque el timer del scheduler escanea vencidos *de todos los tenants*
  en una sola query (`get_due`); el aislamiento ahí es un filtro explícito en
  `list_reminders(tenant_id, ...)` más una verificación de ownership antes de editar/borrar.
  `chats` también quedó sin índice compuesto por tenant/rol (los tenía antes de este cambio):
  ya no son campos escalares, viven dentro de `memberships[]`, y `list_chats_by_role` los
  consulta con `EXISTS(SELECT VALUE m FROM m IN c.memberships WHERE ...)`, que la política de
  indexación por defecto (`/*`) ya cubre.
- **`ToolContext.tenant_id` + `ToolContext.tenant`**: representan el tenant ACTIVO de este
  turno únicamente (nunca "todos los tenants del chat") — cada tool handler tenant-scoped llama
  `require_tenant(ctx)` (en `tools/common.py`) para obtenerlo. `ctx.tenant` trae el documento
  completo del tenant activo (moneda, businessType, itemLabel) para que tools como
  `finance.py` no tengan que volver a leerlo de Cosmos por cada llamada; se arma una sola vez
  por mensaje en `function_app.process_update`.
- **Esquema de negocio genérico**: lo que antes eran "mascotas" ahora es `clients[].items[]`
  con `category`/`attributes` libres — sirve para pets, vehículos, propiedades, lo que
  corresponda al rubro de cada tenant. El system prompt (`prompts.py`) arma el contexto de
  negocio dinámicamente desde `tenants.businessType`/`description`/`currency`/`itemLabel*`, así
  que no hay ramas de código por rubro — toda la especialización vive en los datos del tenant.
- **`PLATFORM_ADMIN_CHAT_ID`** (antes `OWNER_CHAT_ID`) es el chat que recibe alertas de la cola
  envenenada y el que se siembra con `isPlatformAdmin: true` — ya no es "el dueño del negocio",
  es el operador de toda la plataforma.
- **Dar acceso a tu propio usuario en Cosmos DB desplegada**: el Bicep solo otorga el rol
  *Cosmos DB Built-in Data Contributor* a la managed identity de la Function App. Para correr
  `seed_cosmos.py` o cualquier script desde tu máquina contra una cuenta real necesitás
  otorgártelo también:
  ```bash
  MSYS_NO_PATHCONV=1 az cosmosdb sql role assignment create \
    --account-name <cosmosAccountName> --resource-group <rg> \
    --role-definition-id 00000000-0000-0000-0000-000000000002 \
    --principal-id $(az ad signed-in-user show --query id -o tsv) --scope "/"
  ```
  (el `MSYS_NO_PATHCONV=1` es solo necesario en Git Bash en Windows — sin eso, `--scope "/"` se
  reescribe como una ruta de Windows y el comando falla con un error de parseo confuso.)

## Decisiones que no son obvias leyendo el código

- **`main.bicep` es subscription-scope y crea el resource group él mismo** (recurso
  `Microsoft.Resources/resourceGroups`), delegando todo lo demás a
  `modules/resources.bicep` con `scope: rg`. `deploy.ps1` por eso hace un solo
  `az deployment sub create` (no `az group create` + `az deployment group create` por
  separado). Los módulos de recursos (`storage.bicep`, `cosmos.bicep`, etc.) siguen siendo
  resource-group-scope de toda la vida — solo `main.bicep` y `resources.bicep` cambiaron de
  scope/ubicación. Nota: los nombres de deployment a nivel de subscripción son únicos por
  subscripción y quedan atados a la región de su primer uso — `deploy.ps1` usa
  `--name "deploy-$ResourceGroup"` para que desplegar a otra región con otro resource group
  nunca choque con un intento anterior.
- **Plan de hosting = Consumption Y1, no Flex Consumption.** El usuario pidió min 0 / max 1
  instancia literal. Flex Consumption tiene un mínimo de 40 en `maximumInstanceCount` — no
  puede bajar a 1. Y1 sí soporta `functionAppScaleLimit: 1`. Ojo: algunas suscripciones nuevas
  (planes "lab"/trial) arrancan con cuota 0 para "Y1 VMs" en una región dada — si el deploy
  falla con `SubscriptionIsOverQuotaForSku`, o se pide un aumento de cuota en el Portal
  (Portal → Quotas, suele aprobarse al instante en Pay-As-You-Go) o se prueba otra región.
- **Loop de agente manual, no el `tool_runner` beta del SDK de Anthropic.** Se necesita
  interceptar cada `tool_use` para el gating por rol y para escribir el registro de auditoría
  (tokens, latencia, resultado) por cada llamada — el runner del SDK esconde ese punto de
  inspección.
- **Sin `budget_tokens`/thinking ni `effort` en las llamadas a Haiku.** `claude-haiku-4-5` no
  soporta adaptive thinking (usa el formato viejo `{"type":"enabled","budget_tokens":N}`) y
  `output_config.effort` da error en este modelo. No se necesita razonamiento profundo para
  extracción + tool calling, así que se omiten ambos parámetros.
- **`reminders` tiene partition key `/id`** (cada documento es su propia partición lógica) —
  ver "Modelo multi-tenant" arriba para el porqué (el scheduler escanea cross-tenant).
- **Avance de `nextRunAt` con ETag (`try_claim_and_advance` en `repositories/reminders.py`)**
  para que, si el timer llegara a solaparse, un recordatorio no se dispare dos veces.
- **`ToolSpec` y `ToolContext` son dataclasses frozen** — los tests que necesitan reemplazar un
  `handler` para simular un fallo deben usar `dataclasses.replace()` + `monkeypatch.setitem`
  sobre `registry._BY_NAME`, no `monkeypatch.setattr` directo sobre la instancia (ver
  `tests/test_tools.py::test_dispatch_never_raises_on_handler_bug`).
- **`george.config.settings` es un singleton leído al importar el módulo.** Cualquier script
  standalone (seed_cosmos.py, tests/conftest.py) debe poblar `os.environ` **antes** de
  importar cualquier cosa bajo `george.*`, o los valores quedan pegados a lo que hubiera en el
  entorno al momento del primer import.
- **`TELEGRAM_WEBHOOK_PATH` no es `@secure()`** en el Bicep aunque `TELEGRAM_WEBHOOK_SECRET`
  sí — el path es solo una capa de oscuridad y se necesita como output legible para armar la
  URL del webhook; el límite de seguridad real es la comparación de
  `X-Telegram-Bot-Api-Secret-Token` en `telegram.verify_webhook_secret()`.
- **Doble validación de rol**: cada tool handler llama `require_role()` internamente. No es
  redundante con el `allowed_roles` del `ToolSpec` — ese campo es documentación/auditoría, el
  enforcement real está en el handler. Lo mismo aplica a `require_tenant()` para tools
  tenant-scoped.
- **`DRY_RUN=true`** hace que `telegram.py` loggee en vez de llamar a la API real, lo que
  permite correr todo el pipeline (webhook → cola → agente → "respuesta") sin bot de Telegram
  real. `telegram.get_file()` sigue lanzando un error bajo `DRY_RUN` porque no hay archivo real
  que pedir — por eso `scripts/simulate_update.py --voice` inyecta `localAudioPath` en el
  mensaje de cola para saltarse `getFile`/`download` por completo.
- **`agent.py::run_agent` fuerza una llamada extra sin tools si se llega a `MAX_TOOL_TURNS`
  mientras el modelo todavía quería llamar una tool.** Sin esto, la respuesta final del usuario
  podía ser el texto que el modelo escribió ANTES de ver el resultado de esa última tool call
  (planificación tipo "voy a registrar..." en vez de un resumen real, o directamente ocultando
  un error de la tool porque el modelo nunca llegó a leerlo) — un caso concreto de "George dice
  que hizo algo pero no llamó la tool" o al revés. La llamada extra usa `tool_choice: "none"`
  para forzar texto, con los `tool_results` ya en el historial de mensajes.
- **`generate_reminder_message` (recordatorios `kind='prompt'`) arma su system prompt a partir
  del tenant activo** (`name`/`businessType`) en vez de un texto fijo — originalmente decía
  literalmente "un negocio de paseo de perros en Bogota", resto de la primera versión
  single-tenant que quedó sin generalizar. `reminder_worker` en `function_app.py` ahora lee el
  tenant (`tenants_repo.get_tenant`) antes de generar el mensaje.
- **`create_expense` (`tools/finance.py`) es el único movimiento de `finance` que nunca lleva `client_id`** —
  a diferencia de `create_charge`/`register_payment`, que siempre afectan el saldo de un cliente. El doc
  igual guarda `clientId`/`clientName` como `None` explícito (no ausentes) porque `search_finance` indexa
  `r["clientId"]` directo (no `.get()`) al armar su respuesta — un expense sin esa clave rompería esa
  proyección. `get_client_balance` solo suma `type == 'charge'`, así que los expenses nunca contaminan el
  saldo de ningún cliente aunque compartan contenedor/partición. No se agregó un tool `create_adjustment`
  análogo (el tipo `adjustment` queda solo como valor filtrable en `search_finance`, sin escritor) — no se
  pidió, se puede sumar después con el mismo patrón si hace falta.
- **La plantilla de recordatorios que `create_tenant` siembra en cada negocio nuevo vive en
  `platformConfig` (`repositories/platform_config.py`), no hardcodeada en `tools/tenants.py`.**
  El `platform_admin` la lee/edita con `get_default_reminder_templates`/
  `set_default_reminder_templates` — cambia lo que se siembra en negocios FUTUROS, nunca toca
  reminders de negocios que ya existen (esos son del owner, vía `upsert_reminder`/
  `delete_reminder`). Si el documento no existe todavía (deploy viejo, container nuevo sin
  seed), `get_default_reminder_templates` cae a un fallback en código
  (`platform_config.FALLBACK_DEFAULT_REMINDER_TEMPLATES`) para que `create_tenant` no se rompa.
- **CI/CD (`.github/workflows/`) autentica contra Azure con OIDC federado, no un client
  secret.** El service principal (`github-george-bot`) tiene una federated credential atada a un
  subject de environment `production`, así que solo un job que declare `environment: production`
  puede pedir el token — de ahí que `deploy.yml` la ponga en los cuatro jobs que tocan
  Azure/secretos (`infra`, `publish`, `post-deploy`, y el fetch de outputs cuando `infra` se
  salta). Ese mismo environment tiene un required reviewer: el deploy real queda pausado hasta
  aprobarlo a mano en la pestaña Actions.
  - **El subject claim real que este GitHub presenta NO es el formato clásico documentado
    `repo:OWNER/REPO:environment:NAME`** — incluye los ids numéricos de owner y repo:
    `repo:jromero1502@71159797/george-bot@1367655297:environment:production`. La federated
    credential se creó primero con el formato "de libro" y el primer run de `deploy.yml` falló
    en `azure/login@v2` con `AADSTS700213: No matching federated identity record found`. La
    solución no es adivinar el formato — es leer el subject exacto que ya viene en el log del
    job fallido (línea "subject claim - ...") y usar ESE valor literal en
    `az ad app federated-credential update`. Si el repo se transfiere o se borra/recrea, el id
    numérico cambia y hay que repetir el mismo diagnóstico.
  - **`telegramWebhookPath` debe cargarse como GitHub *variable*, no como *secret*.** GitHub
    Actions enmascara automáticamente cualquier output de job que contenga, como substring, el
    valor de un secret registrado — y el output `telegramWebhookUrl` de `main.bicep`
    (`https://.../api/telegram/<path>`) lo contiene literalmente. Cargarlo como secret hace que
    Actions descarte ese output en silencio (`Skip output 'telegramWebhookUrl' since it may
    contain secret`, sin fallar el job), y el siguiente job que lo consume (`post-deploy`, para
    armar la URL del webhook) recibe un string vacío. Coherente con que el propio Bicep ya lo
    trata como no-secreto (ver la nota de `telegramWebhookPath` no `@secure()` más abajo): va en
    `vars`, no en `secrets`, del environment `production`.
- **`main` tiene DOS mecanismos de protección independientes que hay que mirar por separado:**
  la branch protection clásica (`branches/main/protection`, la que gestiona este repo vía API)
  y un **ruleset** (`rs-master`, Settings → Rules), que GitHub sugiere/crea solo al crear un
  repo público y que este repo ya tenía desde antes de este pipeline. Son independientes: pasar
  la clásica no alcanza si el ruleset bloquea, y viceversa. Dos gotchas reales que salieron acá:
  1. El ruleset pedía `required_approving_review_count: 1` — pero **GitHub nunca deja que el
     autor de un PR apruebe su propio PR**, sin excepción y sin ningún setting que lo habilite.
     En un repo de un solo mantenedor eso es un review que no se puede cumplir nunca por la vía
     normal. Se bajó a `0` en el ruleset (junto con `require_code_owner_review: false`, ya que
     no hay `CODEOWNERS`).
  2. El ruleset traía una regla `update` ("Restrict updates") con `bypass_actors: []` —
     bloqueaba **cualquier** actualización de `main` (merge de PR incluido) para **cualquiera**,
     sin relación con el review. `current_user_can_bypass` en la respuesta de
     `GET .../rulesets/{id}` es la señal a mirar: si dice `"never"`, ni admins pueden pasar. Se
     agregó `bypass_actors: [{"actor_type": "RepositoryRole", "actor_id": 5, "bypass_mode":
     "always"}]` (rol `admin`) para que el dueño del repo pueda mergear. `gh pr merge --admin`
     sigue haciendo falta además por la branch protection clásica (que también exige 1 review,
     bypasseable por admins porque `enforce_admins: false`).
- **`deploy.yml` NO reusa `infra/deploy.ps1` tal cual.** La línea que reescribe `$env:Path`
  desde el registro de Windows (para que `func`/`azurite` recién instalados aparezcan en el
  PATH de una sesión ya abierta) es Windows-only y rompería un runner `ubuntu-latest`. El
  workflow llama `az deployment sub create` y `Azure/functions-action` directo;
  `infra/deploy.ps1` queda como el camino de despliegue manual/local únicamente.
- **`infra` (el job de Bicep) solo corre si el diff toca `infra/**`, no en cada merge.**
  `functionapp.bicep` declara `siteConfig.appSettings` como una lista completa (no un merge),
  así que cualquier `az deployment sub create` reescribe `WEBSITE_RUN_FROM_PACKAGE` — la app
  queda momentáneamente sin código hasta que corre `publish`. Filtrar por path evita esa
  ventana en los merges que solo tocan `src/`. El job siempre corre igual y siempre termina
  leyendo los outputs del deployment vía `az deployment sub show` (se salte o no el `create`),
  porque el nombre de la Function App (`uniqueString(resourceGroup().id)`) no se puede
  hardcodear en el workflow.
- **`deployerPrincipalId` (param nuevo en `main.bicep`/`resources.bicep`) es el mismo patrón
  que el role assignment de la managed identity de la Function App, para el service principal
  de CI/CD.** La cuenta Cosmos tiene `disableLocalAuth: true`, así que `seed_cosmos.py` corriendo
  en `post-deploy` necesita RBAC de datos igual que un `az login` humano (ver el comando
  `az cosmosdb sql role assignment create` más abajo) — la diferencia es que este se declara en
  Bicep (vía `sqlRoleAssignments`, condicional a que el parámetro no venga vacío) para que
  sobreviva a un recreate de la cuenta en vez de aplicarse a mano una sola vez.
- **`post-deploy` nunca le pasa `--demo-tenant-owner-chat-id` a `seed_cosmos.py`.** Sin esas
  flags el script es idempotente (`create_*_if_not_exists` en los containers,
  `set_platform_admin` hace merge sin tocar `memberships`) — seguro de correr en cada deploy.
  Con ellas, crearía negocios de demo en producción cada vez.

## Esquema de datos (Cosmos DB, 8 contenedores)

Todos con un campo `custom: {}` libre para extender sin migraciones. `createdAt`/`updatedAt`
en ISO-8601 UTC.

| Contenedor | Partition key | Qué guarda |
|---|---|---|
| `tenants` | `/id` | Un doc por negocio: `name`, `businessType`, `description`, `currency`, `timezone`, `locale`, `itemLabelSingular/Plural`, `status`. |
| `chats` | `/chatId` | Whitelist. `isPlatformAdmin` (bool), `memberships: [{tenantId, role}]` (uno por negocio al que pertenece), `activeTenantId` (cuál está operando ahora), `status`. |
| `clients` | `/tenantId` | Clientes del negocio. `items[]` genérico (`itemId`, `name`, `category`, `attributes: {}` libre, `notes`) en vez de "pets". |
| `finance` | `/tenantId` | Un doc por movimiento (`charge\|payment\|expense`), `clientId`, `balance`, `status`, `currency` (heredada del tenant). `expense` es el único tipo sin cliente — `clientId`/`clientName` quedan `None` (ver nota abajo). |
| `reminders` | `/id` (ver nota arriba) | `tenantId`, `schedule` (`cron`/`once`), `target` (`role`/`chat`), `nextRunAt` (UTC). |
| `pqrs` | `/tenantId` | Peticiones/quejas/reclamos/sugerencias, `reportedBy`, `status`. |
| `conversations` | `/chatId` | Auditoría por turno: tokens, costo, latencia, tool calls. `tenantId` como metadato (no se usa para filtrar, ya está implícito en `chatId`). |
| `platformConfig` | `/id` | Config de plataforma, gestionada por `platform_admin`. Un solo doc hoy (`id: "default_reminders"`): `templates[]` que `create_tenant` siembra en cada negocio nuevo (ver `get_default_reminder_templates`/`set_default_reminder_templates` en `tools/tenants.py`). No afecta negocios ya creados. |

## Comandos útiles

```powershell
# Tests (no requieren Cosmos/Azurite corriendo)
pip install pytest; pytest tests/ -v

# Compilar el Bicep para detectar errores de sintaxis/tipos
az bicep build --file infra/main.bicep --stdout

# Ciclo local completo (siembra platform_admin + un tenant de demo con su owner)
./scripts/local_up.ps1
python scripts/seed_cosmos.py --platform-admin-chat-id 111111111 `
    --demo-tenant-owner-chat-id 222222222
cd src; func start
python scripts/simulate_update.py --chat 222222222 --text "..."

# Para probar un chat que pertenece a DOS negocios (pending_business_selection + switch_business),
# agrega --second-tenant-role-for-owner al seed:
python scripts/seed_cosmos.py --platform-admin-chat-id 111111111 `
    --demo-tenant-owner-chat-id 222222222 --second-tenant-role-for-owner walker
```

## Al modificar tools

1. Agrega el `ToolSpec` en el módulo de dominio correspondiente bajo `src/george/tools/`
   (o crea uno nuevo si es un dominio distinto) — `registry.py` lo recoge automáticamente
   iterando `_MODULES`.
2. El handler llama `require_role(ctx, ALLOWED_ROLES, "tool_name")` primero. Si el tool toca
   datos de negocio (no de plataforma), después llama `require_tenant(ctx)` para obtener el
   `tenant_id` — nunca leas `input_["tenant_id"]` ni nada parecido del tool call, el modelo no
   debería tener forma de elegir el tenant.
3. `input_schema` lleva `additionalProperties: false`. **Los campos opcionales van con un
   tipo simple** (`"type": "string"`, no `"type": ["string", "null"]`) **y fuera de
   `required`** — la API de Anthropic rechaza con 400 un `enum` combinado con un `type`
   en forma de array (`Enum value 'x' does not match declared type '['string', 'null']'`).
   Confirmado en vivo corriendo `func start` local, no es una limitación documentada de
   antemano. `strict` está en `False` por defecto en `ToolSpec` (ver `tools/common.py`): con
   `strict: true` Anthropic limita a 24 el total de parámetros opcionales sumados entre
   *todos* los tools de la request (nosotros tenemos bastantes más entre los 18 tools) — otro
   400 real, no algo que valga la pena resolver fragmentando el tool surface. La validación en
   runtime (`require_role` + `require_tenant` + `ToolError` + `.get()` defensivo en cada
   handler) ya cubre la corrección sin necesitar `strict`.
4. Si el tool escribe en Cosmos, el modelo de datos de esa colección está descrito arriba y en
   los `sqlDatabases/containers` de `infra/modules/cosmos.bicep` — mantenlos en sync si agregas
   un campo nuevo al índice compuesto. Si la colección está particionada por `/tenantId`, el
   `tenant_id` de `require_tenant()` **es** el partition key — pásalo siempre, no lo trates
   como un filtro opcional.
