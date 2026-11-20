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

La ventana de publicación es de 19:00 a 21:00, hora de Madrid, evaluada cuando
empieza el paso de recomendación. Configura el disparo diario existente en
cron-job.org a las 19:00 en `Europe/Madrid`, con el mismo
`POST /repos/{owner}/DeyeCloud/actions/workflows/deye-daily.yml/dispatches`.
Usa la cabecera `Authorization: Bearer <TOKEN>`, `Accept: application/vnd.github+json`,
`Content-Type: application/json` y el cuerpo `{"ref":"main"}` si `main` es
la rama predeterminada. El token dedicado debe tener **Actions: Read and write**
para el repositorio de código. Los horarios de verano e invierno se resuelven
mediante la zona del cron y del cálculo. Comprueba el resultado HTTP en cron-job.org
y el job y su duración en Actions. Un despacho aceptado solo confirma la recepción
de la solicitud. [Configuración de cron-job.org](https://docs.cron-job.org/rest-api.html).

Cada fecha objetivo tiene un único `recommendations/YYYY-MM-DD.json`. Los
reintentos conservan el primer informe, incluso si el informe documenta un fallo
meteorológico. La concurrencia del workflow y el commit no forzado evitan
publicaciones simultáneas. Si falta el informe tras una ejecución fallida dentro
de la ventana, utiliza **Run workflow** con los valores por defecto. Si aparece
`ALREADY_RECORDED`, consulta el informe del día objetivo. Para desactivar la
recomendación, elimina o deshabilita `DEYE_RECOMMENDATION_ENABLED`.

## Previsión y cálculo económico

- **Open-Meteo** aporta una única previsión diaria con series horarias. La
  radiación sobre el plano inclinado y el sol efectivo corresponden a la hora
  anterior al sello. La nubosidad se toma al comienzo del intervalo. La duración
  astronómica y el sol efectivo se registran por separado. Los intervalos UTC se
  convierten a Madrid y conservan el offset en días de 23 o 25 horas. La duración
  astronómica diaria UTC corresponde a la misma jornada solar en Madrid.
  [Documentación de Open-Meteo](https://open-meteo.com/en/docs).
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
- Se parte de un SOC reciente y se estima el SOC de medianoche descontando el
  consumo previsto hasta esa hora. Se simulan carga nocturna, autoconsumo
  directo, carga solar, descarga, importación y exportación. Se consideran
  conversiones, reserva y límites configurables. El escenario de referencia
  conserva los dos objetivos reales, aunque sean distintos o superen los
  niveles candidatos. Se exige superar el ahorro mínimo con las variaciones
  configuradas de consumo y FV antes de recomendar un cambio.
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
a la Fase 2. La aplicación y su verificación corresponden a fases posteriores.

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
