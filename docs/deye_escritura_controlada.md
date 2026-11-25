# Escritura controlada en Deye Cloud

Esta guía se aplica a las nuevas implementaciones de escritura en el inversor.
La referencia probada es [el script de Time 1](../scripts/test_write_deye_time1.py),
que invierte `enableGridCharge` del primer tramo de **System Work Mode-2**.

## Flujo de escritura

1. **Leer y preparar.** Identificar el inversor y el centro de datos. Obtener
   la configuración actual, validar tipos y campos requeridos, y construir la
   petición mínima que admita el endpoint. Mostrar la lectura original, el
   cambio previsto y el cuerpo exacto que se enviará. Si una lectura puede
   estar desactualizada y la operación depende del valor actual, usar una
   lectura fresca para preparar la orden.
2. **Obtener aprobación explícita.** Vincularla al inversor y a la petición
   mostrada. El script de prueba usa `--apply` y exige escribir `APLICAR`.
   Justo antes de enviar, volver a leer la configuración relevante y cancelar
   si ha cambiado desde la vista previa. Las integraciones automáticas deben
   impedir que una aprobación repetida reenvíe la misma operación.
3. **Enviar una sola orden y registrar su `orderId`.** Para los tramos TOU,
   el endpoint es `POST /v1.0/order/sys/tou/update`. En la prueba, Deye
   devolvió el código `1106000` con el mensaje `order sent successfully`.
   Esa respuesta confirma el envío. Conservar el identificador para consultar
   la ejecución. Ante un resultado de envío incierto, comprobar el estado
   disponible antes de decidir si corresponde una nueva operación aprobada.
4. **Esperar el resultado de ejecución.** Consultar
   `GET /v1.0/order/{orderId}`. La consulta puede devolver
   `success: true` mientras la orden sigue pendiente. El campo `status`
   determina el resultado. Usar un límite de espera y registrar cada intento.
   El script hace hasta 25 consultas por orden, con 5 segundos entre llamadas.

| `status` | Estado | Acción |
| --- | --- | --- |
| `0`, `100`, `300` | Pendiente, enviándose o en actualización | Seguir consultando dentro del límite |
| `400` | Terminada | Detener el flujo y registrar el diagnóstico disponible |
| `500` | Fallida | Detener el flujo y registrar el diagnóstico disponible |
| `666` | Ejecutada correctamente | Iniciar la lectura de verificación |

Si se agota la espera, falta el identificador o falla la consulta de estado,
dejar explícito que la ejecución quedó sin confirmar. Registrar el estado HTTP,
el código de aplicación, `requestId`, `msg` y `error` cuando estén disponibles.
Mantener los diagnósticos libres de credenciales, tokens y cabeceras de
autenticación.

## Verificar con una lectura dinámica

Durante las pruebas, `POST /v1.0/config/tou` siguió devolviendo el valor
anterior después de que la orden de escritura llegara a `666`. En varias
consultas por MCP, el valor cambió después de pulsar **Leer** en la web de
Deye. Esa secuencia es compatible con un desfase de actualización. La prueba
con lectura dinámica verificó la escritura desde el propio script.

1. Tras el `666` de la **orden de escritura**, solicitar
   `POST /v1.0/strategy/dynamicControl/read` con `{"deviceSn": "..."}`.
   Deye devuelve un segundo `orderId` para la **orden de lectura**.
2. Esperar el `666` de la orden de lectura mediante el mismo endpoint de
   estado. Si termina con error o se agota la espera, dejar la escritura como
   ejecutada y la verificación como pendiente.
3. Consultar `POST /v1.0/strategy/dynamicControl/readResult` con el
   `orderId` de lectura. Si aún faltan los tramos, el script consulta ese
   mismo resultado hasta 13 veces, con 10 segundos entre consultas. En la
   ejecución verificada, la lectura pasó de `100` a `666` en unos 5 segundos
   y el primer `readResult` devolvió los valores esperados.
4. Comparar los campos devueltos con la petición aprobada y con los valores
   que debían conservarse en la lectura inicial. Normalizar diferencias de
   formato conocidas, como `0000` y `00:00`. Registrar las discrepancias
   por tramo y campo, con el valor esperado y el leído. Identificar cualquier
   campo solicitado que el equipo no devuelva como **sin verificar**.

Para otros parámetros, seleccionar el mecanismo de lectura que devuelva el
estado efectivo del equipo. Validar en el modelo y el firmware de destino qué
campos devuelve la lectura dinámica antes de usarla como comprobación.

## Consideraciones para System Work Mode-2

- Deye exige enviar los **seis tramos en su orden original**. Preservar horas
  y valores de los campos que quedan fuera del cambio aprobado. En el cuerpo
  de escritura las horas deben ir en formato `HH:mm`. El formato `HHmm`
  provocó el rechazo `2101006` durante la prueba.
- `enableSell` es opcional dentro de cada tramo. El script permite omitirlo
  o enviar el booleano `false` en Time 1. Incluirlo explícitamente solicita
  también ese valor, por lo que debe figurar en la vista previa y la
  aprobación. En el inversor probado, la escritura de `enableGridCharge`
  funcionó con ambas variantes, incluida la omisión de `enableSell` en los
  seis tramos. En esa prueba, `enableSell` estaba desactivado antes del
  envío y seguía desactivado después. Queda por comprobar el comportamiento
  al omitirlo cuando está activado y en otros modelos o versiones de firmware.
- El prototipo usa `/v1.0/config/tou` para la vista previa y la comprobación
  previa al envío. En las nuevas escrituras que dependan de un valor previo
  fiable, validar la lectura dinámica también para ese paso.
- Una inversión es sensible a las repeticiones. Para comprobar una orden
  anterior, consultar su estado o solicitar una lectura. Volver a ejecutar
  la prueba con aprobación puede invertir otra vez el valor.

## Pruebas antes de habilitar una escritura

Cubrir con mocks el cuerpo exacto enviado, la cancelación o el cambio previo
a la escritura, la transición de pendiente a `666`, el rechazo y el tiempo
de espera agotado. Comprobar que el resultado dinámico se pide con el
identificador de lectura, que una discrepancia se registra y que ningún
reintento de verificación vuelve a enviar la escritura.

## Integración con la aprobación de los seis tramos

El [workflow privado](../config/deye-approval.workflow.example.json) conserva
el comentario `/deye approve <fecha> <huella>`. Cuando se habilita
`DEYE_TOU_WRITE_ENABLED=true`, las nuevas propuestas ejecutables contienen
el inversor y el JSON completo de la estrategia TOU recomendada. El mismo
evento de aprobación registra la decisión y, después, llama al ejecutor.
Las propuestas emitidas como simulación siguen siendo de simulación.
Configurar la variable y las credenciales Deye en `DeyeCloud`. La ejecución
diaria sincroniza las credenciales, los flags y la plantilla del workflow
privado antes de enviar la propuesta a aprobación. [Configuración de la
sincronización](approval.md#instalación)

El ejecutor comprueba la vigencia de la propuesta y del comentario. Valida
los seis tramos ordenados, los límites de SOC y que la recomendación siga
coincidiendo con la huella aprobada. Bajo el bloqueo comprueba el inversor
conectado, el SOC reciente, la configuración de batería y la lectura
dinámica TOU. Preserva los campos ajenos al plan y omite `enableSell`
cuando la lectura dinámica confirma que está desactivado en los seis
tramos. Si falta esa confirmación, se detiene antes de escribir.

El [bloqueo](../scripts/deye_write_lock.py) se identifica por inversor y se
persiste en `state/inverter-locks/` mediante una actualización Git no forzada.
El flujo de aprobación usa este bloqueo. El prototipo manual y los cambios
desde la web se ejecutan fuera de la reserva. Ejecutar el prototipo solo
cuando no haya una aplicación aprobada en curso para el mismo inversor.

El registro en `approvals/executions/<fecha>/<huella>.json` vincula el
intento a la aprobación y registra los identificadores de escritura y
lectura posterior. La reserva se libera si el preflight rechaza el cambio,
Deye confirma un fallo terminal de la escritura o la verificación coincide.
Si se pierde la respuesta o la lectura difiere, la reserva se conserva.
Antes de liberarla manualmente en el privado, confirmar que no hay un
proceso activo, consultar la orden y reconciliar el resultado con una
lectura dinámica. Conservar el registro de ejecución para impedir que la
misma huella vuelva a enviar una orden.

## Referencias oficiales de Deye

- [OpenAPI EU](https://eu1-developer.deyecloud.com/v2/api-docs)
- [Escritura TOU](https://developer.deyecloud.com/openmcp/docs/deye-open-mcp-tools.html#order_sys_tou_update)
- [Estado de órdenes](https://developer.deyecloud.com/openmcp/docs/deye-open-mcp-tools.html#get_order_result)
- [Lectura dinámica](https://developer.deyecloud.com/openmcp/docs/deye-open-mcp-tools.html#strategy_dynamic_control_read)
