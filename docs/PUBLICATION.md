# Archivos revisados para publicación

Esta lista describe la selección para el primer commit. Se revisaron el contenido
de cada candidato, los datos identificativos presentes en las evidencias locales,
los ejemplos de autenticación, las rutas de máquina y el índice de Git.
Los valores de las pruebas son ficticios y no proceden de las credenciales de Windows.

## Lista publicable

| Archivo | Revisión |
|---|---|
| `.gitignore` | Exclusión por defecto de estado local, informes, secretos y temporales |
| `README.md` | Instrucciones genéricas; sin datos de instalaciones ni rutas personales |
| `requirements.txt` | Dependencias públicas; sin índices privados ni credenciales |
| `config/deye_local.example.toml` | Plantilla con rutas de sustitución explícitas; sin configuración de máquina real |
| `docs/deye_local.md` | Contratos y uso genéricos; retirada la ruta personal y el estado de configuración local |
| `docs/PUBLICATION.md` | Esta lista; solo nombres de archivos y motivos de selección |
| `scripts/setup_credentials.py` | Código de almacenamiento local; sin valores de credenciales embebidos; ejecución explícita |
| `scripts/deye_client.py` | Autenticación en memoria y consultas; sin secretos ni IDs de una instalación real |
| `scripts/deye_read_contracts.py` | Contratos, endpoints y claves de medidas públicos |
| `scripts/deye_mcp_server.py` | Servidor de lectura, validación y errores saneados |
| `scripts/deye_history_quality.py` | Cálculos genéricos de calidad y tiempo; sin muestras reales |
| `scripts/debug_deye_client.py` | Entrada genérica de depuración; consulta datos reales solo al ejecutarse |
| `scripts/audit_deye_readonly.py` | Generador de evidencias locales; no incluye sus resultados |
| `scripts/audit_deye_history.py` | Auditoría temporal genérica; no incluye sus resultados |
| `scripts/audit_deye_history_followup.py` | Diagnóstico limitado; sin datos reales embebidos |
| `scripts/report_deye_history_quality.py` | Generador de tablas a partir de archivos locales excluidos |
| `tests/test_deye_local.py` | Credenciales ficticias, respuestas simuladas y E/S real bloqueada; usa la plantilla pública |
| `tests/test_deye_audit.py` | Respuestas y dispositivos simulados, sin series reales |
| `tests/test_deye_history.py` | Casos de calendario, calidad y límites con datos simulados |

## Archivos del proyecto excluidos

| Archivo | Motivo |
|---|---|
| `config/deye_local.proposed.toml` | Configuración y rutas reales de esta máquina; se conserva localmente |
| `docs/deye-audit-readonly.json` | Datos reales de instalación, dispositivos y telemetría |
| `docs/deye-audit-report.md` | Informe de una instalación real, con identificación y hábitos energéticos |
| `docs/deye-phase4b-results.json` | Resultados de consultas reales y metadatos identificativos |
| `docs/deye-phase4b-followup.json` | Series temporales y dispositivos reales |
| `docs/deye-phase4b-quality.json` | Estadísticas y cobertura del uso energético real |
| `docs/deye-phase4b-hourly.md` | Perfil horario real de consumo, red y batería |
| `docs/deye-phase4b-report.md` | Informe con identificación y datos de la instalación |
| `.agents/skills/VERSION.txt` | Metadatos del paquete de skills instalado localmente |
| `.agents/skills/deye-cloud-scenarios/SKILL.md` | Skill instalada de terceros; ejemplos y datos ajenos al código del proyecto |
| `.agents/skills/deye-monitoring-best-practices/SKILL.md` | Documentación instalada de terceros; se enlaza su fuente oficial |
| `.agents/skills/deye-monitoring-best-practices/agents/openai.yaml` | Configuración de una skill local excluida |
| `.agents/skills/deye-openapi-quickstart/SKILL.md` | Documentación instalada de terceros; se enlaza su fuente oficial |
| `.agents/skills/deye-openapi-quickstart/agents/openai.yaml` | Configuración de una skill local excluida |
| `.agents/skills/deye-openapi-quickstart/references/token-and-control-examples.md` | Ejemplos con cadenas de apariencia sensible; no se redistribuyen en esta selección |

También se excluye cada archivo bajo `.venv/`, `__pycache__/`, `.git/` y `_local/`:
son dependencias instaladas, bytecode, metadatos de Git o material local de revisión.
No se borran los archivos excluidos. `Configuracion.md` y `Consumos.log` están
protegidos por las reglas de exclusión, sin modificarlos.

Las pruebas no necesitan los informes privados, la propuesta local ni las skills
instaladas. La plantilla publicada requiere sustituir sus rutas antes de registrar
un servidor real. Los generadores de auditoría sí necesitan consultar datos reales
o disponer de resultados locales cuando se ejecutan deliberadamente.

La selección se añade al índice por nombres explícitos. Antes del commit se
comprueban el contenido staged y su coincidencia exacta con esta lista. No se
crea un commit ni se publica en un remoto como parte de esta preparación.
