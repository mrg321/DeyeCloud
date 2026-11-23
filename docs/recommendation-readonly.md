# Fase 1 · Recomendación diaria de carga

El workflow diario reutiliza el runner Linux, el entorno `deye-production`, las
dependencias de `requirements-actions.txt` y el acceso a `DeyeCloud-data`. El
colector consulta Deye Cloud EU mediante `deye_client.py` y sus contratos de
lectura. El MCP local expone esas mismas consultas. El colector puede leer
telemetría y, si el dispositivo lo permite, los parámetros TOU, de batería y
del sistema. La lectura de TOU acredita acceso a ese punto de consulta para
el dispositivo, pero la capacidad de aplicar cambios parciales queda sin verificar.

El cálculo consume `daily/`, `intraday/` y `evening/` tras el commit del
colector. La recomendación consulta Open-Meteo y guarda el informe en el
repositorio privado. El paso de recomendación utiliza únicamente el token de
ese repositorio y carece de acceso directo a Deye Cloud. El informe no pasa por
el workspace, los artefactos ni los logs de Actions.

## Configuración y ejecución

1. Guarda la configuración de la instalación en `config/recommendation.json` en
   la rama `main` de `DeyeCloud-data`. Usa [la plantilla pública](../config/recommendation.example.json)
   como referencia. Los campos obligatorios son `latitude`, `longitude`,
   `pv_kwp`, `tilt`, `azimuth`, `battery_nominal_kwh`, `battery_usable_kwh`
   y `grid_kw`.
   `battery_nominal_kwh` es la capacidad nominal de la batería.
   `battery_usable_kwh` es la capacidad efectiva estimada de la batería para
   el rango completo de SOC que muestra el inversor. El modelo descuenta
   la reserva de SOC sobre esa capacidad y registra el resultado en
   `battery_available_above_reserve_kwh`, antes de aplicar las pérdidas de descarga.
   El acimut sigue la convención sur = 0°, este negativo. Incluye solo los paneles
   instalados. Las coordenadas pueden ser aproximadas.
2. Ajusta, si procede, `pv_losses`, `reserve_soc`, `battery_kw`, las eficiencias,
   los niveles de SOC, las tarifas y sus horarios. El calendario anual de festivos
   contiene fechas MM-DD. Los fines de semana y festivos aplicables usan valle todo
   el día. Actualiza el calendario antes de comenzar un año sin cobertura.
   [Circular de la CNMC](https://www.cnmc.es/sites/default/files/2875531_0.pdf),
   [BOE 2026](https://www.boe.es/diario_boe/txt.php?id=BOE-A-2025-21667),
   [BOE 2027](https://www.boe.es/diario_boe/txt.php?id=BOE-A-2026-21254).
3. Prueba manualmente en Actions con **recommendation_dry_run**. Lee los datos
   privados existentes y consulta Open-Meteo. Con GitHub App solicita un token
   `Contents: read`. En modo PAT reutiliza el token existente. El resultado de
   la prueba se reduce a un estado y la duración en el log.
4. Habilita el cálculo persistente con `DEYE_RECOMMENDATION_ENABLED=true`.
   El job sigue sujeto a `DEYE_AUTOMATION_ENABLED=true` y a la rama
   predeterminada. El modo normal reutiliza los Secrets del colector y el token
   `Contents: write` de `DeyeCloud-data` para guardar el informe.
   Consulta los nombres y el alcance en la [guía del colector](automation.md).

La recomendación puede ejecutarse a cualquier hora y siempre apunta al día
siguiente en `Europe/Madrid`. Para generar el informe manualmente, utiliza
**Run workflow** con los valores por defecto. Para el disparo diario existente
en cron-job.org, las 19:00 en Madrid siguen siendo una opción, con el mismo
`POST /repos/{owner}/DeyeCloud/actions/workflows/deye-daily.yml/dispatches`.
Usa la cabecera `Authorization: Bearer <TOKEN>`, `Accept: application/vnd.github+json`,
`Content-Type: application/json` y el cuerpo `{"ref":"main"}` si `main` es
la rama predeterminada. El token dedicado debe tener **Actions: Read and write**
para el repositorio de código. Los horarios de verano e invierno se resuelven
mediante la zona del cron y del cálculo. Comprueba el resultado HTTP en cron-job.org
y el job y su duración en Actions. Un despacho aceptado solo confirma la recepción
de la solicitud. [Configuración de cron-job.org](https://docs.cron-job.org/rest-api.html).

Cada fecha objetivo tiene un único `recommendations/YYYY-MM-DD.json`. Los
resultados también aparecen en la pestaña **Recommendation** del informe
`reports/combined/YYYY-MM-DD.xlsx`, cuyo nombre usa la fecha de recogida en
Madrid. La pestaña indica la fecha objetivo del día siguiente y resume la
previsión, el SOC observado, los objetivos de carga, el consumo, el coste,
el ahorro y el motivo de la decisión. El JSON conserva el detalle horario
y las opciones simuladas. Para cada alternativa, la pestaña muestra la
energía comprada para cargar la batería y el ahorro acumulado frente al TOU
actual. El horizonte abarca el día objetivo completo en Madrid (23, 24 o 25
horas según el cambio horario). Una cifra negativa indica un sobrecoste.

Los reintentos conservan por defecto el primer informe, incluso si documenta
un fallo meteorológico. Para sustituir el informe del día objetivo, abre
**Run workflow** y marca **Recalcular la recomendación y sustituir el informe
guardado** (`recalculate_recommendation`). Deja desmarcado el modo de prueba
para publicar el JSON y actualizar el Excel. Si la aprobación está habilitada,
el flujo privado invalida la propuesta anterior cuando cambia el informe.
La concurrencia del workflow y el commit no forzado evitan
publicaciones simultáneas. Si una ejecución falla y falta el informe, repite el
workflow ese mismo día con los valores por defecto. Si aparece
`ALREADY_RECORDED`, consulta el informe del día objetivo. Para desactivar la
recomendación, elimina o deshabilita `DEYE_RECOMMENDATION_ENABLED`.

## Previsión y cálculo económico

Los candidatos nocturnos predeterminados son `[30, 40, 50, 60, 70, 80]`.
Si la configuración privada ya define `targets`, añade el 30 a esa lista
para evaluarlo. El TOU actual se simula como referencia aunque supere el 80 %.

- `minimum_night_soc` fija el mínimo configurable para proponer un objetivo
  nocturno. Su valor inicial de **30 %** permite evaluar la reserva operativa
  como objetivo de carga. Los candidatos por debajo de la reserva quedan fuera
  y los periodos diurnos conservan su reserva.
- `verified_device_minimum_soc` empieza en `null`. Solo se configura tras
  confirmar el mínimo admitido por el TOU, el inversor y el BMS de la
  instalación, incluidos los límites de reinicio y respaldo que correspondan.
  Para objetivos inferiores al mínimo anterior de 40 %, también se exige
  una lectura actual y coherente de `Low` y `Shutdown`. El objetivo debe
  quedar por encima de ambas protecciones. Si la reserva leída en el TOU
  entra en conflicto con esas protecciones, se bloquea el cambio. El informe identifica un óptimo
  bloqueado aunque pueda recomendar otra opción. La API disponible no
  acredita el mínimo del BMS ni expone todos los límites locales.

  **Nota sobre el 30 %:** el valor configurado se basa en la lectura del TOU
  del inversor y en la documentación pública de Dyness. Dyness no ha confirmado
  ese valor específicamente para la versión de firmware del BMS instalada.
- `reserve_soc` refleja la reserva operativa comprobada en los periodos
  diurnos y `minimum_allowed_soc` combina los límites de la política, el
  equipo y el respaldo. El SOC medido y el SOC proyectado al inicio de la
  carga se muestran por separado. El objetivo nocturno permite cargar si el
  SOC está por debajo durante una franja con carga desde red. Si está por
  encima, la simulación solo descarga para cubrir demanda. Este modelo
  horario no valida el comportamiento exacto del firmware instalado.
  [Manual Deye SUN-SG05LP1-EU](https://www.deyeinverter.com/deyeinverter/2024/02/03/instructions_sun-3.6-8k-sg05lp1-eu_240203_en.pdf).

- **Open-Meteo** aporta la previsión operativa con **ICON-EU** (`models=icon_eu`).
  El informe registra el modelo en `forecast.model`. La
  radiación sobre el plano inclinado y el sol efectivo corresponden a la hora
  anterior al sello. La nubosidad se toma al comienzo del intervalo. La duración
  astronómica y el sol efectivo se registran por separado. Los intervalos UTC se
  convierten a Madrid y conservan el offset en días de 23 o 25 horas. La duración
  astronómica diaria UTC corresponde a la misma jornada solar en Madrid.
  [Documentación de Open-Meteo](https://open-meteo.com/en/docs).
- **ECMWF IFS** (`ecmwf_ifs`) se usa si ICON-EU falla o devuelve datos
  incompletos. La simulación utiliza la serie completa del modelo que superó
  la validación y registra la previsión como no disponible si fallan ambos. Para
  comparar ambos modelos, consulta Single Runs por separado con `models=icon_eu`
  y `models=ecmwf_ifs`, la misma pasada `run` anterior a la recomendación y
  los mismos parámetros de ubicación, inclinación y acimut. Solicita
  `hourly=global_tilted_irradiance`, convierte los intervalos a Madrid y compara
  la energía de ambos modelos con Deye en las mismas horas.
  [Archivo Single Runs](https://open-meteo.com/en/docs/single-runs-api).
- La producción estimada multiplica la irradiación horaria en kWh/m² por la
  potencia pico y el factor de rendimiento. El 14 % inicial agrupa pérdidas de
  temperatura, electrónica, cableado y suciedad como hipótesis de partida. La
  orientación y la pendiente ya intervienen en la irradiación inclinada. Se
  conservan los kWh de FV históricos como referencia para comparar el modelo.
  PVGIS ofrece climatología para contrastar la producción a largo plazo.
  [PVGIS y año meteorológico típico](https://joint-research-centre.ec.europa.eu/photovoltaic-geographical-information-system-pvgis/using-pvgis-5/pvgis-5-tools/pvgis-typical-meteorological-year-tmy-generator_en).
- El consumo se estima con medianas horarias de hasta 12 días, priorizando tipo
  de día y estación. Se integra `TotalConsumptionPower` con muestras de hasta
  diez minutos de separación y se exige un mínimo de 45 minutos observados por
  hora. Se contrasta el resultado con el campo diario `Consumption` y se ajusta
  a ese total si la diferencia está dentro del 35 %. Importación, generación,
  carga y descarga se conservan como magnitudes independientes.
  La simulación reparte la mediana de cada hora según el perfil de 15 minutos
  de un día histórico cercano a la mediana y prueba también el día comparable
  con el mayor pico entre las 05:00 y las 12:00, junto con el escenario de FV
  baja. Cada cuarto requiere al menos diez minutos cubiertos por muestras. Si
  falta cobertura en algún cuarto, esa hora se simula en un único paso. La FV
  prevista se reparte uniformemente dentro de la hora y el informe sigue
  mostrando totales horarios.
- Se parte de un SOC reciente y se estima el SOC de medianoche descontando el
  consumo previsto hasta esa hora. Se simulan carga nocturna, autoconsumo
  directo, carga solar, descarga, importación y exportación. Se consideran
  conversiones, reserva y límites configurables. El escenario de referencia
  conserva los dos objetivos reales, aunque sean distintos o superen los
  niveles candidatos. Se evalúan las mismas variaciones de consumo y FV
  para cada alternativa y para el TOU vigente.
- `current_targets` conserva los dos objetivos observados. `optimal_soc`
  identifica la opción de menor coste del escenario central y
  `recommended_soc` refleja la decisión tras aplicar la política y las
  condiciones de seguridad. Si los dos objetivos difieren, consulta
  `optimal_targets` o `current_targets` para ver ambos valores.
  `gross_saving_eur` conserva el ahorro del óptimo incluso cuando
  `decision_reason` explica por qué se mantiene el TOU actual. La falta de
  perfiles comparables reduce la confianza. Si existen algunos perfiles
  válidos por debajo del mínimo, se muestran los costes y se aplica la política
  conservadora.
- Con precio de importación plano no hay arbitraje horario. La simulación
  cobra la energía comprada para cargar y solo descuenta la energía útil
  descargada al atender consumo. Cada opción registra la energía almacenada
  inicial y final, la carga, la descarga y las pérdidas de conversión.
- `optimization_policy` identifica la política. Primero se busca el menor
  coste entre opciones cuyo ahorro supera `minimum_saving_eur` en todos los
  escenarios. El valor inicial de **0,10 €** evita cambios por diferencias
  pequeñas. Con tarifa plana se descarta aumentar cualquiera de los dos
  objetivos. Una opción que sube un objetivo y baja el otro se trata como aumento.
- Una reducción debe evitar al menos `minimum_avoided_grid_charge_kwh`
  en todos los escenarios. El valor inicial de **2 kWh** exige una reducción
  material. Si ninguna opción supera el umbral económico, una reducción
  puede recomendarse con el motivo `AVOID_UNNECESSARY_GRID_CHARGING`.
  `maximum_grid_reduction_cost_increase_eur` limita el sobrecoste tolerable en
  el peor escenario para esa regla. Empieza en **0 €** y se aplica **0 €**
  también con confianza baja, aunque se configure una tolerancia mayor.
  El informe distingue ahorro monetario, carga evitada y pérdidas evitadas.
- `backup_reserve_soc` y `backup_autonomy_kwh` son restricciones de respaldo
  opcionales. Empiezan en **0**, de modo que la reserva operativa existente
  sigue rigiendo mientras no haya un requisito adicional. La autonomía expresa
  kWh entregables por encima de esa reserva y se convierte a SOC considerando
  la eficiencia de descarga. Si se configura un respaldo, ambos objetivos y
  el mínimo SOC simulado desde las 05:00 deben alcanzar el mayor de los límites
  en todos los escenarios. La comprobación usa el SOC horario simulado y
  conserva los parámetros existentes del inversor.
- Los costes son variables y antes de impuestos. El modelo usa una hora por
  intervalo, aproxima la actuación de TOU y no asigna valor residual a la
  energía al final del día. La capacidad efectiva de la batería, su potencia y
  eficiencias son estimaciones configurables. Si faltan previsión, perfiles,
  calendario, SOC fresco o un TOU compatible, aplica la política `hold`
  o `abstain` y omite cifras económicas que no puede estimar.

### Ejemplo de informe privado

> Fecha objetivo 2026-11-24. SOC observado 40 %. TOU nocturno 40 % y 40 %.
> Recomendación 60 %. Sol efectivo 2,1 h. Producción FV 5,7 kWh. Consumo
> previsto 22,0 kWh. Batería entre las 08:00 y las 24:00 4,8 kWh.
> Coste previsto 2,19 €. Ahorro frente al TOU observado 0,24 €. Confianza media.

Las cifras son ficticias. El JSON guarda las opciones evaluadas, el peor ahorro
por opción, los días usados, la previsión horaria, las incidencias, la huella de
configuración y el commit de origen. La notificación y la aprobación pertenecen
a la [Fase 2](approval.md). La aplicación y su verificación corresponden a fases posteriores.

## Pruebas y consumo de runner

```sh
python -B -m unittest discover -s tests -q
```

Las pruebas simulan respuestas meteorológicas y el almacenamiento privado.
Para 30 disparos mensuales, el colector, la previsión y el cálculo comparten
30 jobs. Como referencia de planificación, 2–4 min por job representan
60–120 min reales al mes, incluidos unos 5–30 min incrementales si cada paso
de recomendación tarda 10–60 s. Son estimaciones pendientes de medición en
Actions. Con un runner estándar y un repositorio de código público, el uso
facturable es cero. Si el código se ejecutara desde un repositorio privado,
se redondearía cada job a minutos enteros hacia arriba y se aplicaría la
cuota correspondiente. Esta fase ejecuta cero aplicaciones y verificaciones.
[Facturación de GitHub Actions](https://docs.github.com/en/billing/concepts/product-billing/github-actions).
# Simulación de seis periodos TOU

La fase 3 reutiliza la captura vespertina, la previsión de Open-Meteo, los perfiles de consumo, la configuración de tarifas y el almacén privado. Se habilita únicamente como simulación con `tou_simulation_enabled: true` en la configuración privada. El valor de ejemplo es `false`. Se conservan el cálculo nocturno y el formato de aprobación anterior, y el informe privado añade `tou_simulation` cuando la opción está activa. Si la revisión móvil está habilitada, muestra la simulación completa en el issue privado. El libro diario incorpora las tres estrategias y sus series horarias.

## Modelo y búsqueda

El horizonte empieza a medianoche de Madrid. Las horas se recorren por tiempo real, con 23 o 25 intervalos en días de cambio de hora. El SOC medido y el SOC proyectado a medianoche se presentan por separado. La proyección usa la carga de vivienda histórica, el TOU de la captura y generación solar cero antes de medianoche. Para dos días se exige una previsión solar completa de confianza media y suficientes perfiles para el tipo del segundo día. Si faltan, se usa un día y se registra el motivo. En dos días se repite el mismo TOU y se aplican las tarifas de cada fecha.

Cada hora asigna la FV al consumo directo y permite almacenar el excedente, con límites de potencia y capacidad. Compra la demanda residual, más la carga de batería autorizada por el Grid Charge simulado. El SOC TOU actúa como suelo de descarga y, con Grid Charge ON, como objetivo de carga. El límite de importación configurado restringe la carga adicional de batería. Un exceso causado por el consumo de vivienda se refleja como importación. Las pérdidas afectan al balance, a las compras y a los excedentes, por lo que ya están incorporadas en el coste. El objetivo resta del coste neto el valor final de la energía utilizable por encima de la reserva. El informe muestra ambos importes.

Un simulador común compara el TOU vigente, una estrategia que solo cambia la carga nocturna y otra que puede ajustar hasta seis periodos. La nocturna conserva los horarios y solo cambia los periodos que cubren exactamente el valle inicial. Si el TOU vigente no permite aislar esa franja, la referencia nocturna conserva el TOU vigente. La búsqueda completa parte del TOU vigente, la mejor solución nocturna y las fronteras de tarifa si hay seis. Explora por coordenadas fronteras vecinas y tarifarias, objetivos SOC y Grid Charge, en pasadas deterministas. Memoriza las simulaciones. El coste es proporcional a los candidatos visitados por las seis coordenadas y las horas por escenario. Se renuncia a la garantía de óptimo global para evitar el producto cartesiano de horarios, SOC y banderas.

Se evalúan el escenario central y cuatro combinaciones de incertidumbre de FV y demanda. La propuesta requiere una mejora de al menos `minimum_saving_eur` en el coste ajustado en todos los escenarios, o evitar al menos `minimum_avoided_grid_charge_kwh` sin encarecer el coste neto ni ajustado. La selección usa el coste ajustado y, para desempatar, la carga desde red, las pérdidas, las compras en punta, la FV almacenada y el número de periodos cambiados. Se evita introducir nuevas ventanas de carga de red en punta. Si el SOC inicial medido o proyectado ya está por debajo de la reserva, una propuesta solo es elegible si recupera la reserva al final de la primera hora en todos los escenarios. El informe calcula el déficit de 18:00 a 22:00 y el SOC teórico para cubrirlo, que puede superar el 100 % o resultar inalcanzable por potencia.

## Parámetros y límites

| Campo privado | Valor de ejemplo | Uso |
|---|---:|---|
| `tou_simulation_enabled` | `false` | Añade el informe de fase 3 |
| `tou_horizon_days` | `1` | Uno o dos días civiles |
| `tou_search_passes` | `3` | Pasadas máximas por punto de partida |
| `tou_minimum_battery_voltage` | `44` | Hipótesis conservadora para convertir amperios a kW. Confirmar el valor de la instalación |
| `tou_terminal_eur_per_kwh` | `null` | Valor por kWh almacenado por encima de reserva. Si es nulo, usa el precio valle configurado |
| `tou_export_limit_kw` | `0` | Límite de exportación FV simulado |
| `tou_credit_export` | `false` | Valora el excedente FV a `export_price` cuando el contrato y el modo lo permiten |

La reserva, la autonomía de respaldo, los objetivos discretos, las eficiencias, las tarifas, el calendario y los márgenes de previsión se toman de la configuración de recomendación existente. La potencia de batería se limita además por las corrientes configuradas leídas. Esas corrientes no reflejan necesariamente la limitación instantánea del BMS por tensión, temperatura o estado de carga. La simulación aplica los límites de potencia en pasos de 15 minutos cuando el perfil histórico tiene cobertura suficiente. Los picos más breves se promedian dentro de cada paso. El modelo tampoco incluye recuperaciones autónomas de seguridad ni venta desde batería. La compensación fija opcional no incorpora límites mensuales, impuestos ni batería virtual. Debe permanecer deshabilitada cuando esas reglas puedan afectar a la decisión.

Solo se simulan cambios en inicio, SOC y Grid Charge. Los seis inicios se interpretan como un ciclo continuo. La última fila acaba en el primer inicio del siguiente día, incluida la representación de medianoche como `0000` en el modelo. Las capturas con granularidad inferior a una hora o con generador se descartan. El fabricante documenta `Power` como un máximo de descarga, por lo que se usa como límite leído y se mantiene fijo por fila. Sell, modo de trabajo, parámetros de seguridad, corrientes y red quedan fuera de los planes. Las lecturas públicas del API y la captura privada permiten confirmar los campos devueltos, pero no verifican el comportamiento de la versión instalada al cambiar parámetros durante un periodo activo.

- [Manual Deye SUN-3.6–8K-SG05LP1-EU](https://www.deyeinverter.com/deyeinverter/2026/01/30/rand/2781/%E3%80%90b%E3%80%91manual_sun-3.6-8k-sg05lp1-eu_20260130_en.pdf)
- [Portal DeyeCloud OpenAPI](https://developer.deyecloud.com/)

## Validación y coste de ejecución

`replay_tou_day` recibe los registros privados diarios e intradiarios y una lectura TOU de referencia. Integra intervalos válidos cortos de potencia y normaliza consumo y FV contra sus totales medidos. Requiere cobertura horaria suficiente y una lectura SOC en los primeros diez minutos. Los resultados son contrafactuales con conocimiento posterior. Si falta el TOU histórico, debe identificarse la fecha del TOU de referencia. La simulación validó días laborables de producción baja y alta y descartó días incompletos. El fin de semana y la batería inicial al 100 % se ejercitan también con datos sintéticos.

La prueba local con 909 candidatos tardó unos 0,32 segundos para 24 h y con 1.328 candidatos unos 0,89 segundos para 48 h. El cálculo nocturno original explora una lista corta de SOC y tiene menor coste de CPU. No se añaden dependencias ni jobs. La latencia de GitHub y Open-Meteo, la instalación de requisitos y el redondeo por job pesan más que el cálculo. Para presupuestar, usar aproximadamente 1–2 minutos por ejecución diaria de colección y recomendación y 1 minuto por cada job de notificación o decisión. Son estimaciones sin ejecución en GitHub Actions. Se mantienen el disparo externo y el flujo sin espera.

## Revisión y activación futura

La revisión de fase 3 fija por hash el informe, la configuración de referencia, los seis periodos propuestos y la caducidad. Se reutilizan la identidad del aprobador, los comandos móviles, la protección contra edición y la idempotencia. Una discrepancia con la última captura privada invalida el voto de aprobación. Una revisión favorable solo registra `simulation_approved`. El proceso no tiene cliente del inversor ni ruta de aplicación.

Antes de una futura activación, hace falta autorización expresa y validar en el equipo la resolución horaria, el cierre a medianoche, Grid Charge, el SOC como límite de descarga, la actuación por debajo del objetivo y los cambios durante un periodo activo. También hacen falta una lectura en directo de TOU, SOC y protecciones justo antes de cualquier operación, comparación con el estado aprobado, comprobación de la ventana horaria y permiso de escritura mínimo. Deben verificarse lectura posterior, auditoría y manejo de errores parciales con un mecanismo de aplicación separado. La aprobación de una simulación no se puede reutilizar como autorización de aplicación.
