# George — asistente de Telegram multi-negocio

George es un bot de Telegram respaldado por un agente Claude Haiku que ayuda a gestionar
negocios de servicios (clientes y sus "items" — mascotas, vehículos, propiedades, según el
rubro —, cartera, recordatorios programados y PQRS). Es **multi-tenant**: un mismo despliegue
puede dar servicio a varios negocios independientes y aislados entre sí, cada uno con su
propio equipo y datos. Corre sobre Azure Functions (Consumption, min 0 / max 1 instancia) y
Cosmos DB (serverless).

## Arquitectura

```
Telegram --HTTP--> telegram_webhook --queue--> process_update --> Cosmos DB
                     (valida y encola)         (resuelve tenant,   (tenants, chats, clients,
                                                 agente Haiku +      finance, records, reminders,
                                                 tools + Groq STT)   pqrs, conversations,
                                                                     platformConfig)

reminder_scheduler --timer 10min--> (avanza nextRunAt) --queue--> reminder_worker --> Telegram
```

- **`telegram_webhook`**: valida el header `X-Telegram-Bot-Api-Secret-Token` y la whitelist de
  `chats`, encola en `george-incoming` y responde `200` de inmediato.
- **`process_update`**: resuelve el rol y el tenant activo del chat para este mensaje (ver
  "Multi-tenancy" abajo) — modo plataforma si es `platform_admin`, selección pendiente si el
  chat pertenece a varios negocios y ninguno está activo, o el negocio único/activo en el caso
  normal; si hay nota de voz, la transcribe con Groq Whisper; corre el loop de Haiku con tools
  sobre Cosmos DB; responde por Telegram; guarda auditoría completa (tokens, latencia, costo,
  tool calls) en `conversations`.
- **`reminder_scheduler`** (cada 10 min): busca recordatorios vencidos **de todos los tenants**,
  avanza su `nextRunAt` (con ETag para evitar dobles disparos) y los encola.
- **`reminder_worker`**: resuelve el/los chat(s) destino **dentro del tenant del recordatorio**,
  redacta el mensaje (con Haiku si el recordatorio es tipo `prompt`) y lo envía.
- **`poison_handler`**: si un mensaje falla 3 veces, avisa al chat `platform_admin`.
- **`health`**: chequeo simple de Cosmos DB.
- **`run_reminders_now`**: dispara el scan de recordatorios manualmente (pruebas).

### Multi-tenancy

Un chat de Telegram puede pertenecer a **varios tenants (negocios) a la vez**, cada uno con su
propio rol — `chats.memberships: [{tenantId, role}, ...]` — y tiene un `chats.activeTenantId`
que indica cuál de esos negocios está operando la conversación en cada momento. La excepción es
el rol especial `platform_admin` (`chats.isPlatformAdmin: true`), que no pertenece a ningún
negocio y es el único que puede asociar chats a negocios:

- **`platform_admin`**: gestiona la plataforma. Sus tools son `create_tenant` (crea un negocio +
  su primer chat `owner` + los recordatorios por defecto de la plataforma),
  `add_chat_to_tenant` (asocia un chat nuevo o ya existente a un negocio ya creado, con
  cualquier rol — es la forma de sumar un chat a más de un negocio), `list_tenants`, y
  `get_default_reminder_templates`/`set_default_reminder_templates` (consultar o cambiar la
  plantilla de recordatorios que `create_tenant` siembra en cada negocio nuevo, guardada en
  `platformConfig`). Se siembra vía `scripts/seed_cosmos.py`.
- **`owner` / `admin` / `walker` / `viewer`**: roles dentro de un negocio. `owner` puede además
  dar de alta/editar chats de su propio equipo con `upsert_chat` (siempre dentro de su propio
  tenant, nunca de otro negocio).
- **Resolución automática por mensaje**: si el chat solo tiene una membership, se usa esa sin
  fricción. Si tiene varias y ninguna está marcada como activa, George entra en modo
  `pending_business_selection` — le pide al usuario elegir un negocio con `list_my_businesses`
  y fija la elección con `switch_business`. Un chat en un solo negocio nunca ve este flujo.

El aislamiento entre negocios es **estructural, no solo un filtro de aplicación**: los
contenedores `clients`, `finance` y `pqrs` están particionados por `/tenantId` en Cosmos DB, así
que una consulta con el `tenantId` equivocado como partition key simplemente no puede
encontrar los datos de otro negocio — no depende de que cada query recuerde agregar un `WHERE`.
Ver la sección "Esquema de datos" más abajo y `CLAUDE.md` para el detalle completo.

El esquema de negocio (clientes + sus "items") es deliberadamente genérico: `items[].category`
y `items[].attributes` son libres, para que el mismo modelo sirva tanto a un negocio de paseo
de perros (`category: "dog"`, `attributes: {breed: "Beagle"}`) como a un taller mecánico
(`category: "sedan"`, `attributes: {plate: "ABC123"}`) o cualquier otro rubro de servicios. El
system prompt de George se arma dinámicamente a partir de `tenants.businessType`/`description`
para que la conversación se sienta específica de cada negocio sin necesitar código distinto
por rubro.

Además, cada negocio puede declarar sus propios **tipos de registro** para llevar en el tiempo lo
que su rubro necesite y que no es ni un cliente/item, ni un movimiento financiero, ni un PQR —
stock, asistencia, ventas del día, mantenimientos, lo que corresponda. El owner los define
conversando con George (`define_record_type`: nombre, campos tipados, si tienen un valor actual
que se reemplaza o son eventos que se acumulan, y por qué agruparlos); el equipo carga datos con
`log_record` y consulta el histórico o el estado actual con `search_records`/`summarize_records`.
George detecta proactivamente cuándo el owner está describiendo esta necesidad y le propone
definir la estructura antes de asumir nada. Un reporte también puede salir solo: un recordatorio
`kind="report"` corre el agente con acceso de solo lectura a los registros y manda el resultado
armado con datos reales, sin poder escribir nada. Ver `CLAUDE.md` para el detalle del modelo de
datos (`records`, particionado por `/tenantId`).

Ver `infra/` para el detalle de la infraestructura (Bicep) y `CLAUDE.md` para las decisiones de
arquitectura.

## Requisitos

- Python 3.11
- [Azure Functions Core Tools v4](https://learn.microsoft.com/azure/azure-functions/functions-run-local)
- [Azurite](https://learn.microsoft.com/azure/storage/common/storage-use-azurite) (`npm install -g azurite`)
- [Azure Cosmos DB Emulator](https://learn.microsoft.com/azure/cosmos-db/emulator) (nativo en Windows)
- Azure CLI (`az`) + extensión Bicep, solo para desplegar
- Una API key de [Anthropic](https://console.anthropic.com/) y una de [Groq](https://console.groq.com/)
- Un bot de Telegram (token de [@BotFather](https://t.me/BotFather)) — opcional para desarrollo local (`DRY_RUN=true`)

## Desarrollo local

```powershell
# 1. Levanta Azurite + verifica el emulador de Cosmos
./scripts/local_up.ps1

# 2. Entorno Python
cd src
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
cd ..

# 3. Configuración local
Copy-Item src/local.settings.json.example src/local.settings.json
# edita ANTHROPIC_API_KEY y GROQ_API_KEY con valores reales

# 4. Crea el esquema en el emulador, siembra el chat platform_admin y (opcional)
#    un tenant de demo con su owner + recordatorios por defecto, todo en un paso
python scripts/seed_cosmos.py --platform-admin-chat-id 111111111 `
    --demo-tenant-owner-chat-id 222222222 --demo-tenant-owner-name "Maria"

# 5. Arranca la Function App
cd src
func start
```

En otra terminal, simula mensajes sin necesidad de un bot real (`DRY_RUN=true` hace que
`sendMessage` solo quede registrado en el log de `func start`):

```powershell
# Como platform_admin: pedirle a George que cree un negocio nuevo (alternativa a --demo-tenant-* del seed)
python scripts/simulate_update.py --chat 111111111 --text "crea un negocio de peluqueria canina llamado Pelos y Colas, el owner es el chat 333333333 y se llama Ana"

# Como owner de un tenant (usa el chatId sembrado con --demo-tenant-owner-chat-id, o el que haya creado platform_admin)
python scripts/simulate_update.py --chat 222222222 --text "cuanto me debe Maria"
python scripts/simulate_update.py --chat 222222222 --voice ./samples/nota.ogg   # requiere GROQ_API_KEY real
python scripts/simulate_update.py --text "hola" --chat 999999999               # chat no autorizado -> se descarta

# Dispara el scan de recordatorios sin esperar los 10 minutos
curl -X POST http://localhost:7071/api/ops/run-reminders
```

### Probar con un bot real desde localhost (opcional)

```powershell
devtunnel host -p 7071 --allow-anonymous
# o: ngrok http 7071

./scripts/set_webhook.ps1 -BotToken "<token>" `
    -FunctionAppUrl "<url-del-tunel>/api/telegram/local-dev-path" `
    -FunctionKey ""  `  # func start no exige function key en local
    -Secret "local-dev-secret"
```

Recuerda poner `DRY_RUN=false` en `local.settings.json` para que las respuestas salgan de
verdad por Telegram.

## Tests

```powershell
pip install pytest
pytest tests/ -v
```

`test_scheduling.py` cubre el cálculo de `nextRunAt` (cron diario/semanal, `once`, cruce de
DST, expresiones inválidas). `test_tools.py` cubre el gating por rol, el aislamiento
multi-tenant (`require_tenant`) y el rechazo de inputs inválidos en las tools — ninguno de los
dos requiere Cosmos DB corriendo.

## Despliegue

**A `main` llega todo por PR, y cada merge despliega solo a producción vía GitHub Actions**
(`.github/workflows/deploy.yml`) — ver "CI/CD" más abajo. `infra/deploy.ps1` sigue existiendo
como camino manual/local (por ejemplo para levantar un entorno nuevo de cero o depurar un
deploy), pero ya no es el camino de producción.

```powershell
az login
az bicep upgrade   # opcional, para tener la última versión

# Copia infra/main.parameters.json a un archivo local con valores reales
# (no lo commitees) y despliega:
./infra/deploy.ps1 -ResourceGroup rg-george-bot -ParametersFile ./infra/main.parameters.local.json
```

El script crea el resource group (el template es subscription-scope), despliega `main.bicep`
(storage + Cosmos DB serverless + Function App Consumption + diagnostic settings) y publica el
código. Al final imprime los siguientes pasos: obtener la function key, registrar el webhook
(`scripts/set_webhook.ps1`) y sembrar Cosmos DB (`scripts/seed_cosmos.py`, corriendo con tu
propia sesión de `az login` — necesitas que tu usuario tenga el rol **Cosmos DB Built-in Data
Contributor** en la cuenta, ya que la identidad administrada de la Function App no es algo que
puedas usar para correr un script desde tu máquina; ver el comando `az cosmosdb sql role
assignment create` en `CLAUDE.md`).

Tras el primer despliegue, solo sembrás el chat `platform_admin` (`--platform-admin-chat-id`,
sin `--demo-tenant-*`); los negocios reales se crean conversando con George como
`platform_admin` y usando `create_tenant`.

### CI/CD (GitHub Actions)

- **`.github/workflows/ci.yml`** — corre en cada PR hacia `main` (y en `main` mismo): tests
  (`pytest tests/ -v`, herméticos, sin Cosmos/Azurite) + `az bicep build`/`lint` sobre
  `infra/main.bicep`. Sin secretos ni `id-token: write` — un PR desde un fork no puede tocar
  Azure. `main` tiene branch protection: ambos checks son obligatorios antes de mergear.
- **`.github/workflows/deploy.yml`** — corre en cada push a `main` (post-merge). Se autentica
  contra Azure con **OIDC federado** (`azure/login`, sin client secret) contra el environment
  `production` de GitHub, que tiene un **required reviewer** — el deploy real queda pausado
  hasta que alguien lo aprueba desde la pestaña Actions.
  1. `infra`: redespliega `main.bicep` **solo si cambió algo bajo `infra/`** (o en el primer
     run) — cada `az deployment sub create` reescribe `appSettings` como lista completa, así
     que redesplegar sin necesidad borraría `WEBSITE_RUN_FROM_PACKAGE` sin motivo. Siempre lee
     de vuelta los outputs del deployment (el nombre de la Function App es
     `uniqueString(resourceGroup().id)`, no se puede hardcodear).
  2. `publish`: publica `src/` con `Azure/functions-action` (build remoto vía Oryx —
     equivalente a `func azure functionapp publish --python`, que no es portable a un runner
     Linux porque `deploy.ps1` reescribe `$env:Path` desde el registro de Windows).
  3. `post-deploy`: siembra Cosmos (`seed_cosmos.py`, idempotente, **nunca** con
     `--demo-tenant-*`), registra el webhook de Telegram (`scripts/set_webhook.ps1`, corre bien
     en `pwsh` sobre Linux) y hace un smoke test contra `GET /api/health` (anónimo, reintenta
     hasta ~90s) antes de dar el deploy por bueno.
  - `workflow_dispatch` con el input `force_infra` fuerza el redeploy de infra sin que haya
    cambiado nada bajo `infra/` — útil para recuperarse de un estado raro a mano.

### Variables de entorno / app settings

| Variable | Descripción |
|---|---|
| `COSMOS_ENDPOINT`, `COSMOS_DATABASE` | Cosmos DB. Sin `COSMOS_KEY` en Azure — se usa la identidad administrada. |
| `COSMOS_KEY` | Solo local, contra el emulador. |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`, `TELEGRAM_WEBHOOK_PATH` | Telegram. El secreto real es el header `X-Telegram-Bot-Api-Secret-Token`; la function key y el path son capas adicionales. |
| `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL` | Claude Haiku (`claude-haiku-4-5` por defecto). |
| `GROQ_API_KEY`, `GROQ_STT_MODEL` | Whisper (`whisper-large-v3-turbo` por defecto). |
| `DEFAULT_TIMEZONE` | Fallback cuando un tenant no especifica su propia zona horaria. |
| `PLATFORM_ADMIN_CHAT_ID` | ChatId del operador de la plataforma — recibe alertas de la cola envenenada y es quien crea negocios nuevos. |
| `HISTORY_TURNS` | Cuántos turnos previos se cargan como contexto del agente (12 por defecto). |
| `DRY_RUN` | `true` para loggear en vez de llamar a la API de Telegram. |

## Esquema de datos

Ver la sección "Esquema de datos" de `CLAUDE.md` para el detalle completo de las 9
colecciones de Cosmos DB (`tenants`, `chats`, `conversations`, `clients`, `finance`, `records`,
`reminders`, `pqrs`, `platformConfig`) — todas con un campo `custom: {}` libre para extender
sin migraciones.

## Limitaciones conocidas

- **Cold start**: la primera invocación tras inactividad puede tardar 3–8s (Consumption +
  Python). El webhook responde rápido igual (solo encola); lo que puede demorar es la
  *respuesta* de George.
- **Sin Application Insights**: no hay live metrics ni KQL. La auditoría en `conversations`
  es la fuente principal de observabilidad; los diagnostic settings quedan como blobs JSON de
  respaldo en el storage account (con expiración a 90 días).
- **Procesamiento serial**: `functionAppScaleLimit: 1` + `batchSize: 1` en las colas significa
  que los mensajes se atienden uno a la vez, **de todos los tenants combinados**. Correcto para
  un puñado de negocios pequeños; si el volumen crece, subir `batchSize` primero.
- **Un chat en varios negocios opera uno a la vez.** No hay una vista combinada entre negocios
  ni acciones cross-tenant en un mismo mensaje — el chat elige un negocio activo con
  `switch_business` y opera solo sobre ese hasta que pida cambiar (ver `CLAUDE.md`).
