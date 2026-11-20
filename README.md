# DeyeCloud MCP local de solo lectura

Cliente de Deye Cloud EU y servidor MCP stdio para consultar instalaciones,
dispositivos, telemetría, alarmas, parámetros e históricos documentados.
No expone órdenes de control, cambios de configuración ni HTTP arbitrario.

## Instalación en Windows

Requiere Python 3.11 o posterior. Desde la raíz del repositorio:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe scripts/setup_credentials.py
```

El último comando es un configurador **local y explícito**: solicita los datos
sin mostrarlos y guarda o sustituye las cuatro entradas de Deye Cloud EU en
Windows Credential Manager. No hace consultas de red y no se ejecuta desde el
servidor MCP. Las credenciales no se almacenan en archivos del repositorio.

## Servidor y pruebas

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -u scripts/deye_mcp_server.py
```

El servidor espera mensajes MCP por stdin; stdout se reserva al protocolo.
Las pruebas usan respuestas ficticias y bloquean red y credenciales reales.
Los scripts de auditoría consultan datos reales cuando se ejecutan expresamente.

- [Herramientas, parámetros, límites y auditorías](docs/deye_local.md).
- [Ejemplo de registro MCP](config/deye_local.example.toml).
- [Lista de archivos revisados para publicación](docs/PUBLICATION.md).
- [Automatización diaria con datos en repositorio privado](docs/automation.md).
- [Previsión solar y recomendación de carga en solo lectura](docs/recommendation-readonly.md).

La automatización usa el cliente directamente desde Linux y GitHub Actions
Secrets, manteniendo la autenticación local de Windows. Está deshabilitada hasta
autorización explícita. El código público y los datos privados residen en
repositorios distintos; no se suben informes ni consumos como artefactos de Actions.

Los resultados de auditoría pueden contener nombres de instalaciones, IDs,
versiones, consumos y patrones de uso aunque oculten los números de serie.
Esos resultados, la configuración local, las skills instaladas, el entorno
virtual y las cachés están excluidos de Git. Este repositorio contiene código,
documentación genérica y pruebas simuladas; no incluye datos de una instalación.
