# Fase 2 · Aprobación móvil

La [Fase 3](deye_escritura_controlada.md#integración-con-la-aprobación-de-los-seis-tramos)
añade la aplicación de propuestas TOU ejecutables tras el mismo comentario
de aprobación del Issue privado.

El workflow diario guarda la recomendación privada y envía un
`repository_dispatch` a `DeyeCloud-data` con la fecha objetivo. Allí, un
workflow crea y asigna un Issue privado a las cuentas autorizadas. GitHub
notifica la asignación según las preferencias de cada cuenta. El job termina
y la espera usa **cero minutos de runner**. Un comentario nuevo válido
dispara otro job que registra la decisión. Una decisión aprobada autoriza
únicamente los cambios incluidos en la propuesta. En la Fase 2, el registro
es el resultado final y la etapa de aprobación se ejecuta sin credenciales Deye.

## Elección del mecanismo

| Mecanismo | GitHub Free y experiencia móvil | Seguridad, infraestructura y coste |
|---|---|---|
| Issues privados | Acceso desde GitHub Mobile o navegador. Notificación de asignación y respuesta mediante comentario. | Identidad de GitHub, lista de IDs y verificación de contenido. Dos ejecuciones cortas, sin infraestructura adicional ni runner durante la espera. |
| Environments con required reviewers | Revisión desde la pantalla de ejecución en navegador. En GitHub Free los revisores obligatorios están disponibles en repositorios públicos. | GitHub protege el job antes de asignarle runner. La revisión del despliegue no ofrece por sí sola el registro privado y la vinculación de cada decisión a la propuesta. |
| Telegram | Buena interacción móvil mediante botones. | La recepción de callbacks exige un endpoint HTTPS y autenticación del webhook, o consultas periódicas de actualizaciones. Añade infraestructura o ejecuciones y otro canal que custodiar. |

Se eligieron Issues en el repositorio privado existente para conservar juntas
las propuestas, las decisiones y su historial, con menos infraestructura.
[Eventos de Actions](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows),
[GitHub Environments](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/review-deployments),
[Telegram webhooks](https://core.telegram.org/bots/webhooks).

## Instalación

1. Publica el código en `mrg321/DeyeCloud` y habilita Issues y Actions en el
   repositorio privado `mrg321/DeyeCloud-data`. En el entorno
   `deye-production` de **`DeyeCloud`**, configura el secreto
   `DEYE_APPROVAL_WORKFLOW_TOKEN` con un token dedicado a `DeyeCloud-data`
   con **Contents: write**, **Workflows: write**, **Variables: write** y **Secrets: write**.
   Cada ejecución normal usa esta credencial para sincronizar
   [la plantilla](../config/deye-approval.workflow.example.json) con
   `.github/workflows/deye-approval.yml` en `main` y reemplaza el archivo si difiere.
   El workflow privado usa el código de `main` de `mrg321/DeyeCloud`.
   [Permisos de la API de contenido de GitHub](https://docs.github.com/en/rest/repos/contents#create-or-update-file-contents).
2. Configura en las variables de Actions de **`DeyeCloud`**
   `DEYE_APPROVER_IDS` como un array JSON de IDs numéricos de GitHub
   y `DEYE_APPROVER_LOGINS` como un objeto JSON de ID a login. Ejemplo ficticio
   `[12345]` y `{"12345":"octocat"}`. Puedes consultar el ID con
   `gh api users/LOGIN --jq .id`. Otorga acceso y comprueba que cada cuenta
   puede ser asignada a Issues del repositorio. El worker verifica la
   correspondencia entre login e ID antes de crear la notificación.
3. Configura opcionalmente `DEYE_APPROVAL_CUTOFF` en **`DeyeCloud`**
   con formato `HH:MM`. Por defecto es `23:00` de Madrid del día anterior a la
   fecha objetivo. Si la variable está sin definir, el worker usa ese valor
   y lo sincroniza al privado si la variable de origen está vacía.
   El cambio de hora se resuelve al crear cada propuesta.
4. Establece `DEYE_APPROVAL_ENABLED=true` en **`DeyeCloud`** cuando quieras
   activar el flujo. El repositorio de código también necesita las variables
   de habilitación de la [Fase 1](recommendation-readonly.md). El token de datos
   existente, con **Contents: write** en el privado, envía el despacho privado.
   Cada ejecución normal sobrescribe en el privado las variables de
   aprobación y `DEYE_TOU_WRITE_ENABLED` desde `DeyeCloud`. También
   actualiza `DEYE_APP_ID`, `DEYE_APP_SECRET`, `DEYE_EMAIL` y
   `DEYE_PASSWORD` desde los secretos de `deye-production`. El token de
   sincronización se usa solo en el repositorio de origen.
   Si la sincronización falla, el flujo no envía la recomendación a aprobación.
   El worker usa su propio `GITHUB_TOKEN` con **Contents: write** e **Issues:
   write** en el privado. Comprueba que las restricciones de Actions y de la
   rama permiten que ese token guarde la auditoría. GitHub genera el
   `GITHUB_TOKEN` del worker. Los cuatro secretos Deye los recibe la etapa
   de escritura del repositorio privado.
   [API de repository_dispatch](https://docs.github.com/en/rest/repos/repos#create-a-repository-dispatch-event).

El cron externo conserva el disparo de `deye-daily.yml`. El evento privado
de recomendación y el comentario humano se gestionan en GitHub
independientemente de cron-job.org. El modo de prueba de la Fase 1 omite la
sincronización y el despacho privado.

Para sincronizar manualmente solo el archivo del workflow se puede usar
`./scripts/Install-DeyeApprovalWorkflow.ps1` desde PowerShell con `gh`
autenticado y permisos de escritura de workflows. `-WhatIf` comprueba el destino
sin escribir.

## Uso desde el móvil

- Activa las notificaciones de GitHub Mobile y las notificaciones de
  asignaciones en tu cuenta. Abre el Issue privado de la fecha objetivo.
  También puedes ver las propuestas pendientes en **Issues → Open** de
  `DeyeCloud-data`. Consulta la hora límite que aparece en el Issue.
  [Preferencias de notificaciones de GitHub](https://docs.github.com/en/account-and-profile/managing-subscriptions-and-notifications-on-github/setting-up-notifications/configuring-notifications).
- El Issue muestra, entre otros datos, el SOC actual y recomendado, sol
  efectivo, producción estimada, consumo, aporte de batería, ahorro,
  confianza y motivo de la recomendación. Cuando hay un cambio aprobable,
  copia uno de los comandos completos que ya aparecen en el Issue y
  envíalo en un comentario nuevo. Usa `/deye approve …` para aprobar,
  `/deye reject …` para rechazar o `/deye cancel …` para cancelar. El comando
  incluye fecha y huella íntegra de la propuesta.
- Los informes que recomiendan mantener la configuración o abstenerse
  generan un Issue informativo. El registro `no_change` cierra el Issue.
  Una propuesta creada después de la hora límite se registra como caducada
  en el mismo flujo.
- Consulta la decisión en `approvals/decisions/<fecha>/<huella>.json` en el
  privado. El registro conserva el identificador de cuenta, el comando,
  la hora del comentario y el resultado. El Issue se cierra después del
  commit de auditoría. Un Issue abierto puede haber caducado si todavía
  no ha llegado otro evento.

## Caducidad, integridad y recuperación

El worker conserva el contenido aprobado en `approvals/proposals/` y el
identificador activo por fecha en `approvals/current/`. Verifica el ID del
comentario y del remitente, vuelve a leer comentario e Issue de GitHub y
comprueba el hash de la recomendación privada. La fecha del comentario y
la hora de procesamiento deben preceder al límite. El límite actúa al
instante y las caducidades pendientes se registran al llegar un comentario
tardío o el próximo despacho diario, sin procesos de espera. Una nueva
propuesta invalida la anterior para esa fecha. El commit no forzado evita
sobrescribir decisiones simultáneas.

Si se interrumpe la publicación tras crear el Issue, un despacho diario
repetido el mismo día puede recuperar el Issue del bot. Una decisión ya
registrada nunca se sustituye. Si falló el cierre del Issue después del
commit, consulta primero el registro privado y cierra el Issue si procede.
Si un comentario tardó en procesarse o un job fue reemplazado en la cola,
consulta **Actions** del repositorio privado y confirma el estado de auditoría.
Antes de la hora límite puedes reenviar un comentario nuevo si el anterior
carece de decisión registrada. Deshabilita `DEYE_APPROVAL_ENABLED` en
`DeyeCloud` para dejar de emitir propuestas. Para bloquear también las
decisiones sobre propuestas pendientes, deshabilita el workflow privado.

Para estimar el consumo, con 30 notificaciones y hasta 30 decisiones
mensuales hay hasta 60 jobs privados adicionales. Si cada job dura 20–45 s,
son 20–45 minutos reales, redondeados a unos 60 minutos de cuota mensual.
Son estimaciones pendientes de medir en producción. La espera añade cero
minutos y esta fase realiza cero aplicaciones y verificaciones del inversor.
[Facturación de Actions](https://docs.github.com/en/billing/concepts/product-billing/github-actions).

Las pruebas offline se ejecutan con
`python -B -m unittest discover -s tests -q`. Incluyen decisiones válidas,
rechazo, cancelación, caducidad, duplicados, ediciones, usuarios ajenos,
propuestas sustituidas y reintentos de publicación.

En Windows puedes reproducir las tres etapas con
`./scripts/Invoke-DeyeWorkflowSimulation.ps1`. El lanzador usa el entorno
virtual del repositorio y ejecuta la recogida, la recomendación y la aprobación
con datos y eventos simulados. No necesita `.env.collector` ni credenciales.
La prueba de aprobación pasa por el mismo punto de entrada que Actions con
un despacho y un comentario, y verifica la decisión en un repositorio simulado.
