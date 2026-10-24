# Auditoria de ingreso

Pagina: `/auditoria`. Configuracion: `/settings/audit`.

## Configuracion y consulta

- Las credenciales son las del webservice SIAL de UTI/UNRC. No son las de Moodle, Teams ni el acceso personal a SIAL. Se guardan en `.env`, no se muestran en HTML ni se exportan. Campos vacios al guardar conservan los valores existentes.
- Configurar inicio y cierre de la campana. Sin cierre, o antes de finalizar ese dia, las bajas estan bloqueadas. Cada auditoria conserva sus propias fechas; cambiar configuracion no cambia informes anteriores.
- Realizar auditoria consulta preinscriptos, aspirantes e ingresantes desde la fecha inicial, cruza el DNI con `aDNI@dominio`, consulta Moodle, equipos directos y los dos informes Microsoft de 180 dias. No crea usuarios, matriculas ni envia correos.
- El trabajo continua en el servidor aunque se cierre la pagina. El historial y el progreso permiten volver a verlo. Solo se ejecuta una auditoria/baja a la vez dentro de este modulo.
- Los errores se conservan como datos incompletos, nunca como ausencia de actividad. Una auditoria parcial puede descargarse; las personas sin identidad comprobada no pueden seleccionarse para baja.
- Los informes Microsoft tienen su propia fecha de actualizacion. No incluyen todos los accesos Entra ni equipos accesibles solamente por canales compartidos.

## Seleccion y bajas

- Preinscripto: condicion SIAL unica y ausencia verificada de cursos Moodle. Cualquier matricula, incluso de otra carrera o anio, lo excluye de la baja en ambos sistemas. Una cuenta Moodle inexistente verificada tambien cumple la ausencia de cursos; un error de consulta no. Sin cuentas en ambos sistemas no hay baja que preparar. Actividad o equipos Microsoft no excluyen este criterio. Es una baja de cuenta, no una desmatriculacion.
- Los filtros `Preinscriptos sin cursos` (candidatos) y `Preinscriptos con cursos` (no seleccionables) separan estos casos. Los candidatos se muestran incluso durante una campana abierta, pero las casillas solo se habilitan al finalizar la auditoria y tras el cierre. Las auditorias guardadas y sus Excel se evaluan con el criterio vigente sin repetir la consulta.
- Aspirante: sin cursos Moodle, sin equipos directos y sin actividad Microsoft informada en 180 dias. El acceso historico a Moodle no lo excluye. No se infiere incumplimiento academico: la API SIAL no informa documentos ni actividades pendientes.
- Seleccionar filas, la pagina completa, o todos los candidatos del filtro. La seleccion de pagina no incluye otras paginas; la opcion de todo el filtro si las incluye.
- Preparar baja guarda una vista previa local. No modifica servicios externos. La confirmacion exige la palabra `ELIMINAR`, el reconocimiento del alcance y la contrasena del operador en el CRM.
- Al ejecutar se vuelven a consultar los estados SIAL y las identidades de ambas cuentas. Una cuenta recreada con otro ID bloquea la baja. Una nueva inscripcion fuera de la campana tambien la bloquea.
- Para todos los candidatos se vuelve a verificar que no haya cursos Moodle antes de eliminar cualquiera de las cuentas, incluso en lotes preparados con reglas anteriores. Los lotes que no cumplen el criterio vigente se bloquean. Para aspirantes ademas se renuevan informes y equipos. Se exige una cuenta de al menos 180 dias e informes con hasta 7 dias de retraso. Una respuesta incompleta o actividad reciente bloquea la eliminacion.
- Se elimina la cuenta completa Microsoft 365, no solamente Teams. Puede afectar correo, archivos y otros servicios asociados. La cuenta Moodle tambien se elimina.
- Se registra el resultado de cada sistema, DNI, operador y hora. Reintentar solo pendientes no repite bajas ya confirmadas. Si una cuenta ya no existe, queda resuelta como `missing` sin repetir una eliminacion.
- El registro local se desactiva solo tras confirmar el resultado remoto correspondiente. No se borran auditorias anteriores ni sus evidencias.
- No hay borrados programados ni automaticos. `CRM_DRY_RUN=true` impide las bajas reales.

## Persistencia y reinicios

Las tablas `audit_*` se crean en la base SQLite del CRM al iniciar. Cada consulta guarda un SQLite independiente en `data/auditorias_crm/<id>/auditoria.sqlite3`. Incluir esa carpeta, la base principal y `.env` en las copias privadas de seguridad.

El Excel se genera al descargar y no sobrescribe archivos anteriores. Incluye personas, condiciones, candidatos, casos con/sin uso, carreras, cursos, equipos, informes de actividad, incidencias y bitacora de bajas. Las anotaciones hechas en un Excel no se importan automaticamente.

Si Windows interrumpe una tarea, tras vencer su bloqueo (30 minutos sin progreso) queda marcada como interrumpida. Las auditorias se repiten con una nueva consulta. Las bajas parciales se reintentan explicitamente, con las verificaciones actuales. No se reanudan eliminaciones por el solo hecho de encender la PC.

## Verificacion local

`python -m unittest discover -s tests` incluye pruebas con bases temporales y servicios simulados, nunca elimina cuentas reales.

Prueba visual opcional: instalar Playwright en el entorno de desarrollo y ejecutar `python tests/audit_browser_check.py`. Usa Edge headless, un servidor temporal con datos ficticios y bloquea solicitudes externas. Capturas: `data/test_artifacts/audit/`.
