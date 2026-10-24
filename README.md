# CRM-FCE

CRM local para importar listados de estudiantes o docentes desde Excel, crear cuentas Microsoft 365 por Microsoft Graph, asignar licencias A1, matricular en Moodle por nombre corto de curso y enviar credenciales por correo.

## Estado

Esta primera version queda preparada para trabajar en modo seguro:

- Login local para `ivan`, `marianela`, `matias` y `guillermo`.
- Importacion Excel `.xlsx`.
- Reglas de cuentas:
  - Estudiantes: `a{documento}@eco.unrc.edu.ar`.
  - Docentes: inicial del primer nombre + apellido completo, sin espacios ni acentos, `@eco.unrc.edu.ar`.
- Modo simulacion por defecto (`CRM_DRY_RUN=true`).
- Clientes configurables para Azure Graph, Moodle REST y SMTP.
- Logs de cada importacion y detalle de cuentas existentes o con error.
- Procesamiento en lote en segundo plano con progreso en vivo.
- Pestanas de gestion local de `Estudiantes` y `Profesores` con filtros por DNI y curso.
- Desde esas pestanas se puede desmatricular por curso o eliminar usuarios en bloque de Microsoft/Moodle.

## Instalacion

1. Instalar Python 3.12 o superior.
2. Crear entorno virtual:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

3. Crear configuracion:

```powershell
Copy-Item .env.example .env
```

4. Editar `.env`.
5. Levantar servidor:

```powershell
.\start_crm.ps1
```

Tambien podes iniciarlo/detenerlo con doble clic:

- `start_crm.bat`
- `stop_crm.bat`

Desde la misma PC: `http://localhost:8000`.
Desde otra PC de la red Windows: `http://IP-DE-LA-PC:8000`.
Para lotes grandes, ajustar `CRM_BATCH_WORKERS` en `.env` (ejemplo: `5` a `10`).

Para detenerlo:

```powershell
.\stop_crm.ps1
```

## Usuarios locales

Se crean automaticamente estos usuarios si no existen:

- `ivan`
- `marianela`
- `matias`
- `guillermo`

La clave inicial sale de `CRM_DEFAULT_PASSWORD`. Cambiarla en `.env` antes de usar en produccion.

## Excel esperado

Columnas recomendadas:

- Estudiantes: `documento`, `nombre`, `apellido`, `email`
- Docentes: `nombre`, `apellido`, `email`

Tambien reconoce variantes comunes: `dni`, `numero_documento`, `nro_documento`, `nombres`, `apellidos`, `correo`.
Si el listado trae una sola columna de nombre, puede usar `apellido y nombre`, `nombre completo`, `alumno`, `docente`, `profesor` o variantes similares. Con coma se interpreta como `Apellido, Nombre`.

El nombre corto del curso de Moodle se carga en el formulario de importacion.
Al subir el archivo, el CRM muestra una vista previa y permite asociar manualmente cada columna del Excel con los campos internos antes de procesar.
Se pueden indicar varios cursos separados por coma, por ejemplo: `9040,9041`.

## Azure / Microsoft 365

No se guarda una clave de Administrador Global. Lo recomendado es registrar una aplicacion en Microsoft Entra ID y darle consentimiento de administrador para Microsoft Graph.

Permisos de aplicacion necesarios:

- `User.ReadWrite.All` para crear usuarios.
- `LicenseAssignment.ReadWrite.All` para asignar licencias.

Completar en `.env`:

- `AZURE_TENANT_ID`
- `AZURE_CLIENT_ID`
- `AZURE_CLIENT_SECRET`
- `AZURE_STUDENT_SKU_ID` (licencia para estudiantes)
- `AZURE_TEACHER_SKU_ID` (licencia para docentes)

Para validar codigos de licencia se puede usar Graph `GET /subscribedSkus` o desde la aplicacion en `Licencias`.

## Moodle

Activar Web Services REST en Moodle y crear un token con funciones:

- `core_course_get_courses_by_field`
- `core_user_get_users_by_field`
- `core_user_create_users` si `MOODLE_CREATE_MISSING_USERS=true`
- `enrol_manual_enrol_users`
- `enrol_manual_unenrol_users` para desmatriculacion por curso desde el CRM

El CRM busca el curso por `shortname` y matricula con rol estudiante/docente segun el tipo importado.
Si se habilita `MOODLE_CREATE_MISSING_USERS=true`, crea el usuario en Moodle antes de matricular y usa como email el mismo username institucional.

## Correo

Configurar SMTP de Google Workspace en `.env`. Si la cuenta tiene 2FA, usar clave de aplicacion o el mecanismo definido por el administrador de Workspace.
Tambien se puede configurar desde la pestana `Correo` en la app, incluyendo asunto, plantilla y concurrencia del procesamiento por lotes.
En cada importacion se puede personalizar asunto y mensaje masivo, con variables:

- `{full_name}`
- `{upn}`
- `{password}`

## Notas de seguridad

La pestana **Auditoria** consulta SIAL, Moodle y Microsoft 365, guarda informes SQLite y exporta Excel. Las bajas requieren seleccion, confirmacion y revalidacion. Configuracion, criterios y recuperacion: [docs/auditoria.md](docs/auditoria.md).

La pestana **Ingreso** separa la actualizacion SIAL, las cuentas Microsoft con correo de bienvenida y las matriculas Moodle sin correo. Se entrega en modo preparacion, con operaciones reales bloqueadas independientemente del resto del CRM. Campanas y reglas de Contabilidad: [docs/ingreso.md](docs/ingreso.md).

- Mantener `.env` fuera de backups compartidos.
- Mantener `CRM_DRY_RUN=true` hasta verificar reglas de nombres y licencias.
- Con `CRM_DRY_RUN=true`, cada importacion queda en modo simulacion: calcula usuarios y registra resultados, pero no crea cuentas en Azure, no matricula en Moodle y no envia correos.
- Las contrasenas temporales se generan por usuario y no se guardan en claro en logs.
- En produccion conviene poner el CRM detras de VPN o solo en red interna.
# repo_tempo_y3
