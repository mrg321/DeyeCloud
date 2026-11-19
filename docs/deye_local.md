# DeyeCloud: MCP local de lectura y auditoría técnica (0.4.1, fase 4B)

Para ejecución diaria sin MCP y separación entre código público/datos privados,
véase [automatización con GitHub Actions](automation.md). El modo local conserva
Windows Credential Manager; el cliente también admite un modo explícito de
variables de entorno, sin fallback cuando están incompletas.

## Archivos

- `scripts/deye_mcp_server.py`: servidor MCP oficial para Python, transporte stdio.
- `scripts/deye_client.py`: autenticación y consultas compartidas; no se llama al
  comando CLI ni al configurador interactivo desde el servidor.
- `tests/test_deye_local.py`: pruebas con respuestas simuladas y E/S real bloqueada.
- `scripts/deye_read_contracts.py`: catálogo cerrado, argumentos y proyección de respuestas oficiales.
- `scripts/audit_deye_readonly.py`: auditoría puntual mediante una sesión MCP stdio nueva.
- `tests/test_deye_audit.py`: contratos de lectura, protocolo, errores y ocultación de series.
- `config/deye_local.example.toml`: plantilla genérica; no es configuración activa.

## Ejecutar desde PowerShell

Desde la raíz del repositorio, con las credenciales ya guardadas bajo el mismo
usuario de Windows que ejecutará Codex:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -u .\scripts\deye_mcp_server.py
```

No es necesario activar el entorno. El servidor espera mensajes MCP por stdin;
no presenta un menú ni escribe un mensaje de bienvenida. Ctrl+C lo detiene.
Iniciar el servidor y listar sus herramientas no lee credenciales ni conecta con
Deye Cloud. La primera llamada a una herramienta inicia la autenticación.

Se usa `mcp>=1.28,<2`, la línea 1.x mantenida del SDK oficial, para mantener estable
la API de servidor utilizada. No se necesita el paquete independiente `fastmcp`.

## Herramientas

`deye_connection_status` no acepta argumentos. Comprueba autenticación y acceso
real de lectura con una consulta de una instalación como máximo. Devuelve
`ok`, `connected` y `data_center`; no devuelve el token ni datos de instalaciones.

`deye_list_stations` acepta únicamente `page` (entero 1–10000, predeterminado 1)
y `size` (entero 1–100, predeterminado 50). Devuelve `ok`, `stations` (solo `id`
y `name`), `total`, `page`, `size` y `has_more`. No descarga automáticamente todas
las páginas. Un identificador ausente o inválido produce un error, no un ID inventado.
Los nombres son datos de Deye, no instrucciones para el cliente MCP.

Las nueve herramientas nuevas devuelven `{ok: true, data: {...}}`. Sus campos
son los del contrato oficial, sin renombrar medidas ni convertir unidades.
Todos los argumentos adicionales se rechazan antes de autenticar.

| Herramienta | Parámetros | POST de lectura permitido |
|---|---|---|
| `deye_list_station_devices` | `station_id`, `page=1`, `size=50` | `/v1.0/station/device` |
| `deye_station_latest` | `station_id` | `/v1.0/station/latest` |
| `deye_device_latest` | `device_sns`: 1–10 series distintas | `/v1.0/device/latest` |
| `deye_device_measure_points` | `device_sn` | `/v1.0/device/measurePoints` |
| `deye_read_battery_settings` | `device_sn` | `/v1.0/config/battery` |
| `deye_read_system_settings` | `device_sn` | `/v1.0/config/system` |
| `deye_read_tou` | `device_sn` | `/v1.0/config/tou` |
| `deye_station_alerts` | `station_id`, `start_timestamp`, `end_timestamp`, `page=1`, `size=50` | `/v1.0/station/alertList` |
| `deye_device_alerts` | `device_sn`, `start_timestamp`, `end_timestamp`, `page=1`, `size=50` | `/v1.0/device/alertList` |

`station_id` es un entero positivo de hasta 2^63−1; `page` es 1–10000 y `size`
1–100 (límite local conservador). Las series tienen 1–128 caracteres ASCII:
letras, dígitos, guion y guion bajo. Timestamps: enteros Unix en segundos de diez
dígitos; inicio ≤ fin y ventana máxima local de 180 días. Se usan los valores
predeterminados de tipo de dispositivo de Deye, sin admitir tipos arbitrarios.
`measure_points` se utiliza en la auditoría únicamente para INVERTER, su tipo
predeterminado documentado.

Respuestas principales:

- Dispositivos: `deviceListItems` con `deviceSn`, `deviceType`, `productId`,
  `connectStatus`, `collectionTime`, `deviceId`, `parentId`, `stationId`.
- Telemetría: `deviceDataList` con `collectionTime` (Unix segundos), `deviceState`
  (1 online, 2 alerta, 3 offline), `deviceSn`, `deviceType` y `dataList` de
  `key/value/unit`. Los nombres de medidas son datos dinámicos oficiales.
- Resumen de instalación: `batterySOC`, potencias de batería, carga, descarga,
  consumo, generación, red, compra y cable, irradiancia y `lastUpdateTime`.
- Medidas admitidas: `measurePoints`, `productId`, `deviceType`, `deviceSn`, `timeZone`.
- Batería: `battCapacity`, `battLowCapacity`, `battShutDownCapacity`,
  `maxChargeCurrent`, `maxDischargeCurrent`.
- Sistema: `energyPattern`, `systemWorkMode`, `maxSellPower`, `maxSolarPower`,
  `zeroExportPower`.
- TOU: `touAction` y `timeUseSettingItems` (`enableGeneration`,
  `enableGridCharge`, `power`, `soc`, `time`, `voltage`). Se conserva el horario
  leído; no se prepara ni envía una orden.
- Alarmas: `stationAlertItems` o `alertList`, `total`, `page`, `size`, `has_more`.
  Se conservan código, nombre, inicio, fin, nivel (0 aviso, 1 advertencia,
  2 fallo), estado (1 abierto, 0 cerrado) e impacto; las alarmas de dispositivo
  incluyen descripción, razón y solución si Deye las devuelve.

Los campos opcionales ausentes se representan como `null`, sin sustituirlos por
cero. Una lista vacía es distinta de una colección ausente; en listados paginados
solo se normaliza la lista ausente cuando el total oficial es cero. Las respuestas
con tipos incompatibles fallan con `INVALID_RESPONSE`. Campos no documentados,
incluidos `msg`, `requestId` y cualquier secreto, se excluyen de la proyección.
Las unidades solo se muestran cuando constan en `unit` o en una fuente oficial;
los parámetros numéricos de configuración se conservan sin atribuirles unidades
no especificadas en el esquema. No existe un endpoint de firmware o modelo exacto
verificado: esas versiones solo se recogen si aparecen en la telemetría.

Errores de herramienta: `isError=true` con códigos fijos como `INVALID_ARGUMENTS`,
`UNKNOWN_TOOL`, `BUSY`, `DEYE_UNAVAILABLE`, `INVALID_STATION_ID` o
`RESPONSE_TOO_LARGE`. No se incluyen excepciones originales ni cuerpos HTTP.
También se distinguen `AUTH_REJECTED`, `ACCESS_DENIED`, `RATE_LIMITED`,
`DEVICE_OFFLINE`, `UNSUPPORTED_DEVICE`, `UNSUPPORTED_CONFIG_POINT`,
`DEVICE_NOT_FOUND_FOR_OPERATION` e `INVALID_RESPONSE`. Los errores de aplicación
solo se traducen mediante una tabla cerrada; los desconocidos son
`DEYE_UNAVAILABLE`. `DEVICE_NOT_FOUND_FOR_OPERATION` no demuestra que el equipo
esté físicamente ausente: puede fallar únicamente ese servicio.

## Límites y confidencialidad

- Solo se permiten autenticación POST `/v1.0/account/token`, sin bearer previo,
  y los endpoints POST de lectura enumerados en este documento, con bearer. Los
  endpoints `/config/*` permitidos son consultas oficiales de parámetros.
  No existen herramientas para escribir parámetros, HTTP libre, comandos,
  estrategias dinámicas, archivos o credenciales. Tampoco se permite añadir
  parámetros query a consultas autenticadas.
- Reutiliza `read_and_validate_credentials()` y su decodificación UTF-16LE con
  `win32cred.CredRead`. No ejecuta `setup_credentials.main()` ni `CredWrite`.
- La contraseña original se transforma mediante SHA-256 UTF-8 hexadecimal antes
  de enviar el JSON. `appId` va en la URL; `appSecret`, `email` y `password` en JSON.
- Token únicamente en memoria, con reutilización de hasta 30 minutos o una ventana
  menor según `expiresIn`. No se guarda refreshToken. Al cerrar el proceso se
  descartan las referencias; Python no garantiza borrado físico de memoria.
- Una consulta simultánea como máximo; otras llamadas reciben `BUSY`. No hay
  reintentos automáticos. Un fallo invalida la sesión local y la siguiente
  llamada vuelve a autenticar.
- TLS verificado, redirecciones rechazadas, timeout de conexión de 5 s y lectura
  de 10 s. Presupuesto de 30 s por petición comprobado entre bloques; una lectura
  bloqueada puede añadir hasta su timeout. Cada herramienta hace como máximo dos
  peticiones (autenticación si hace falta y una página). Codex dispone de 120 s.
- Respuesta HTTP descomprimida limitada a 1 MiB, nombres a 256 caracteres,
  strings de telemetría a 4096 caracteres, colecciones a 2000 elementos y
  resultado de herramienta a 512 KiB de JSON ASCII (ampliado para curvas intradía).
  Los límites excesivos fallan
  de forma explícita, sin devolver datos parcialmente truncados.
- stdout se reserva al transporte MCP. Solo se emiten mensajes fijos en stderr.
  El arranque desactiva logs de dependencias y depuración HTTP para que no puedan
  registrar cabeceras, URLs ni excepciones. No activar captura de variables o
  volcado de memoria en producción. Los identificadores y nombres solicitados sí
  salen del proceso como resultados MCP.
- Como el cliente existente, no toma proxies ni credenciales `.netrc` del entorno.
  El flujo implementado es el token personal; no incorpora el flujo empresarial
  adicional de `companyId`.

## Pruebas sin Deye Cloud

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v
```

Las pruebas interceptan `CredRead`, `CredWrite`, el envío HTTP y la creación de
conexiones. Usan credenciales ficticias, respuestas HTTP en memoria y una sesión
MCP oficial conectada por streams en memoria. Cubren contratos de autenticación,
reutilización del token, catálogo y llamadas MCP, argumentos inválidos, errores
HTTP/TLS/timeout/aplicación, salida sin secretos, proyección de nombres e IDs,
paginación, concurrencia, límites y rutas TOML. Una prueba adicional inicia el
servidor real por stdio en un subproceso con la E/S de red y credenciales también
bloqueada, comprueba el catálogo y verifica que los fallos no emitan tracebacks.

## Registro local del servidor

Copiar `config/deye_local.example.toml` a un archivo local excluido de Git y
sustituir `C:\PATH\TO\DeyeCloud` por la ubicación real del proyecto. Añadir únicamente
su sección `[mcp_servers.deye_local]` a la configuración existente del cliente MCP.
Conservar las demás secciones; no reemplazar el archivo completo del cliente.

Este cambio no registra el servidor ni altera configuración MCP activa.

Un proceso MCP ya iniciado mantiene el código anterior hasta que se reinicie o reconecte.
La auditoría se ejecuta mediante un proceso stdio nuevo, sin depender del catálogo
almacenado por la conversación actual y sin utilizar `deye_open`.

## Auditoría puntual

```powershell
.\.venv\Scripts\python.exe -B scripts/audit_deye_readonly.py
```

Exige exactamente una instalación visible. Consulta estado, inventario, telemetría,
medidas, parámetros de lectura y alarmas de los últimos siete días. Conserva el
resultado proyectado en `docs/deye-audit-readonly.json`, ocultando las series
antes de escribir. El JSON conserva el nombre de instalación e IDs internos.
Los informes humanos generados localmente también deben conservarse fuera de Git.

Consulta secuencial, sin polling ni reintentos; separa las llamadas al menos 2,1 s
para reservar margen a la autenticación. Un 429 impide nuevas consultas durante
esa ejecución. Inventario y alarmas: máximo diez páginas; detalles: máximo cien
dispositivos, lotes de telemetría de diez. Los límites, cambios de total, páginas
repetidas y resultados incompletos se señalan, sin declararlos auditoría completa.
Una ventana sin alarmas no permite descartar alarmas anteriores ni fallos físicos.

## Verificación documental

Contratos contrastados el 17-11-2026 con la referencia oficial enlazada abajo y
el memorando de parámetros de la skill `deye-cloud-scenarios` v261114. También se
revisaron `deye-openapi-quickstart` y `deye-monitoring-best-practices` v261114.
La comprobación remota de versión de skills no estuvo disponible; se conservaron
las instaladas. SHA-256 del HTML oficial consultado:
`F901C986843DD06189FAEBBF44FFD504068AEDAF14B2E569F2A8887974B89CD0`.

La skill antigua describe `itemList` en una nota, mientras el esquema oficial
actual documenta `dataList`; se implementa `dataList`, confirmado además por la
respuesta real. No se implementan endpoints deducidos a partir de nombres,
comandos de lectura asíncrona de estrategias ni registros Modbus adivinados.

## Fuentes

- Skills oficiales consultadas (instalación local opcional, excluida del repositorio):
  [DeyeCloud skills](https://developer.deyecloud.com/mcp-skills).
- Endpoint y paginación:
  [documentación oficial Deye](https://developer.deyecloud.com/openmcp/docs/deye-open-mcp-tools.html#station_list).
- [SDK oficial MCP Python 1.x](https://github.com/modelcontextprotocol/python-sdk/tree/v1.x).
- [Configuración MCP de Codex](https://developers.openai.com/codex/mcp/).

## Fase 4B: históricos y calidad temporal

Se añaden cinco herramientas (16 en total):

| Herramienta | Argumentos obligatorios | Operación oficial y semántica |
|---|---|---|
| `deye_station_metadata` | `station_id` | POST `/v1.0/station/detail`; solo nombre, ID, estado y `regionTimezone` |
| `deye_station_daily_history` | `station_id`, `start_date`, `end_date` | POST `/v1.0/station/history`, `granularity=2`; fechas locales inclusivas |
| `deye_station_power_history` | `station_id`, `start_timestamp`, `end_timestamp` | POST `/v1.0/station/history/power`; curva por Unix segundos |
| `deye_device_daily_history` | `device_sn`, `start_date`, `end_date` | POST `/v1.0/device/history`, `granularity=2`; no envía `measurePoints` |
| `deye_device_intraday_history` | `device_sn`, `date`, `measure_points` | POST `/v1.0/device/history`, `granularity=1`; un día local y medidas seleccionadas |

Las fechas son `YYYY-MM-DD` y se validan como fechas reales del calendario.
Los resúmenes diarios se limitan localmente a siete fechas inclusivas; no aceptan
intervalos invertidos. Las curvas por timestamps requieren fin > inicio, con
duración máxima de 25 horas para admitir el día del cambio al horario de invierno.
No se envían parámetros `page`/`size` a los históricos: los esquemas HTTP publicados
no los incluyen. Si `total` no coincide con el número de registros, la respuesta
indica `quality.complete_by_total=false`; nunca se presume que hay cobertura total.

`measure_points` admite entre 1 y 15 claves distintas de un catálogo verificado:
`SOC`, `BMSSOC`, `TotalDCInputPower`, `TotalConsumptionPower`, `TotalGridPower`,
`BatteryPower`, `DC Temperature`, `AC Temperature`, `DailyActiveProduction`,
`PVDailyPowerGenerationActive`, `DailyConsumption`, `DailyEnergyPurchased`,
`DailyGridFeedIn`, `DailyChargingEnergy`, `DailyDischargingEnergy`.
La auditoría consulta primero `measurePoints` y solicita únicamente las claves
que ese inversor declara disponibles. No se aceptan claves, cuerpos o endpoints libres.

Respuestas de instalación: `stationDataItems`, `total` y diagnóstico local de
recuento. Se proyectan los seis campos diarios de energía `generationValue`,
`consumptionValue`, `purchaseValue`, `gridValue`, `chargeValue`, `dischargeValue`,
potencias, SOC, marcas de tiempo y componentes de fecha cuando existen.
Los valores diarios de energía están documentados en kWh en la skill oficial;
el esquema de curva de potencia no especifica unidades y no se les atribuyen W
ni kW por semejanza numérica.

Respuestas de dispositivo: `dataList` con `time` e `itemList` (mapas de strings),
`deviceSn`, `deviceType`, `deviceId`, `granularity`. Se conservan solo claves de
medida verificadas y `key/value/unit`. Los campos ausentes no se convierten en
cero y los tipos incompatibles siguen dando `INVALID_RESPONSE`. Las excepciones
de las consultas pueden incluir `schema_issues`: nombres de campos
documentados y tipos esperados/recibidos, sin sus valores ni respuestas crudas.

Fuentes: referencia oficial enlazada arriba, secciones `station_history`,
`station_history_power`, `device_history`, `station_detail`, y memorando de la
skill `deye-cloud-scenarios` v261114. Hay una contradicción entre el texto corto
del MCP web ("1 diario") y el memorando detallado ("2 diario, 1 intradía"). Se usa
este último con cuerpos explícitos y se comprueban las fechas y granularidad
devueltas. No se implementa `device_history_raw`: el documento revisado describe
su herramienta pero no verifica inequívocamente su ruta HTTP en las skills.

### Ejecución y archivos de fase 4B

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -B scripts/audit_deye_history.py
```

`scripts/audit_deye_history.py` abre un MCP stdio nuevo y usa exclusivamente
`deye_local`. El presupuesto es 44 llamadas MCP, hasta 88 peticiones HTTP contando
autenticaciones, separadas al menos 2,1 segundos. No hay reintentos automáticos;
un 429 o rechazo de autenticación/acceso detiene las consultas pendientes.
Solo admite la instalación única y un inversor; un inventario con páginas
pendientes se declara incompleto. Usa siete fechas terminadas en la fecha local
de la última telemetría disponible, acotadas por el momento de consulta. El día
actual se marca parcial. No busca indefinidamente siete días con datos: distingue
los siete días solicitados de los realmente recuperados.

`docs/deye-phase4b-results.json` contiene resultados MCP proyectados, series
ocultas, cobertura, errores y estadísticas por hora. El informe se entrega en
`docs/deye-phase4b-report.md`. Se preservan los artefactos de la auditoría anterior.
El script no abre ni escribe `docs/Configuracion.md` o `docs/Consumos.log`.

Para diagnosticar una ejecución ya guardada se dispone de
`scripts/audit_deye_history_followup.py`: lee las evidencias anteriores y limita
las nuevas consultas al presupuesto **restante**, con un máximo adicional de seis.
Guarda `docs/deye-phase4b-followup.json`, sin reemplazar los resultados iniciales.
En esta fase se recuperaron curvas con el formato documentado `startAt=endAt`;
el cliente ya envía ambos campos. En ejecuciones futuras el inventario de días
del resumen diario evita consultar detalles de fechas anteriores sin resumen,
manteniendo una consulta del último día por si su agregado aún no está disponible.

`scripts/report_deye_history_quality.py` calcula tablas y cobertura **sin red**,
desde los archivos saneados: `docs/deye-phase4b-quality.json` y
`docs/deye-phase4b-hourly.md`. El informe distingue las evidencias de la primera
ejecución, sus incompatibilidades y las lecturas recuperadas en el diagnóstico.

Los errores documentados de argumentos de Deye `2101006`, `2101008` y `2101012`
se traducen como `API_ARGUMENT_REJECTED`, `API_ARGUMENT_TYPE_REJECTED` y
`API_INTERVAL_REJECTED`. Nunca se incluyen mensajes crudos de validación.

### Reglas de tiempo y unidades

- `zoneinfo` y `tzdata` usan reglas IANA para `Europe/Madrid`. Se generan límites
  de día mediante medianoches locales sucesivas: días de 23, 24 o 25 horas.
- Se comprueba `regionTimezone` contra Madrid, por hora, para toda la ventana.
  Nombres de zona distintos pueden tener los mismos offsets durante ese intervalo;
  no se cambia la zona configurada en Deye.
- Marcas con offset o Unix segundos se convierten a UTC y luego a Madrid.
  Horas locales sin offset requieren una zona fuente. Las horas repetidas o
  inexistentes del cambio horario se marcan ambiguas/inválidas, sin adivinar.
  La hora repetida con offset explícito conserva ambas instancias por separado.
- La API no especifica inclusividad del extremo de curva: se solicita hasta el
  segundo previo al límite y el análisis aplica `[inicio, fin)`, sin doble conteo.
- Se señalan duplicados, valores ausentes, SOC fuera de 0–100, unidades cambiantes,
  muestras fuera de intervalo, intervalos observados y huecos frente a la
  referencia documentada de cinco minutos. Un hueco no equivale a cero.
- Las medias horarias son medias de muestras, no medias ponderadas ni energía.
  La diferencia entre dos lecturas de un contador kWh cubre solo el tramo observado
  entre ambas; se suprime si decrece. No se convierte esa diferencia en energía
  completa de una hora, ni se integra potencia sin garantizar unidades/cobertura.
- Los kWh diarios son energía acumulada durante el día; W/kW son potencia;
  SOC es porcentaje. Los campos `purchaseValue` y `ElectricityPurchasing` tienen
  en la skill una advertencia sobre la exclusión de carga de batería desde red:
  no se suman automáticamente como si representaran todo el intercambio físico
  ni se usan para forzar un balance entre fuentes distintas.

Las pruebas de fase 4B cubren protocolo MCP real con respuestas simuladas,
peticiones exactas, fechas inválidas, intervalos de 25 horas, cambios de horario
de marzo/octubre, duplicados, huecos, ausencia de unidades, incompatibilidades,
resets de contador, límites y ocultación de series. Toda E/S real está bloqueada.
