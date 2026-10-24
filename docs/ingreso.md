# Ingreso

Pantallas: `/ingreso`, `/settings/ingreso`. No utiliza el conversor Lincu de matriculacion semestral.

## Estado inicial: preparacion

`INGRESO_ALLOW_WRITES` es `false` por defecto, independientemente del modo productivo del resto del CRM. La interfaz y el servidor bloquean crear cuentas, asignar licencias, matricular, modificar grupos y enviar correos. Consultar SIAL y preparar vistas previas solo realiza lecturas externas y guarda resultados locales.

No se habilito la ejecucion real durante el desarrollo. Su activacion futura requiere autorizacion explicita, revisar las reglas y los destinatarios, `INGRESO_ALLOW_WRITES=true`, `CRM_DRY_RUN=false` y una clave `CRM_SECRET_KEY` segura de al menos 32 caracteres. No activar estas opciones para probar con alumnos reales.

## Campanas y actualizaciones SIAL

- Crear una campana con fechas de inscripcion inclusivas. El formulario propone Ingreso 2027, diciembre 2026 a marzo 2027. Las fechas se pueden editar incluso despues de consultar, siempre que no haya una tarea de Ingreso en proceso. El cambio marca los datos como pendientes de actualizar e inhabilita las vistas previas pendientes de ambos procesos. Pulsar Actualizar desde SIAL y preparar nuevas vistas previas. Conserva el historial y las operaciones confirmadas; no modifica Microsoft ni Moodle. Para separar temporadas se recomienda crear otra campana.
- Comparte las credenciales del webservice SIAL configuradas en Auditoria, pero no sus fechas ni sus criterios de baja.
- Consulta primero preinscriptos, luego aspirantes e ingresantes para reducir huecos al avanzar de condicion durante la consulta. No es una transaccion atomica del sistema remoto. Unifica por DNI y conserva condiciones por inscripcion/carrera, sin elegir una condicion global que oculte otras carreras.
- Las tres listas deben llegar completas antes de reemplazar la vista local. No visto en una consulta no implica baja: conserva la persona y sus inscripciones previas, pero bloquea acciones sobre ella hasta volver a verificarla.
- Guarda nombre, apellido, email y telefono del endpoint de datos personales, comprobando el DNI. Los errores no se convierten en identidades inexistentes.
- Las instantaneas de inscripciones, las tareas y sus resultados se guardan en las tablas `ingreso_*` de SQLite. Las contrasenas no aparecen en vistas, Excel ni bitacoras.

## Cursos y Contabilidad

La API observada informa ID de carrera, nombre, fecha de inscripcion, modalidad y una lista de comisiones (ID/nombre). No informa I1/I2 ni una catedra contable. El numero de comision no equivale al numero de aula Moodle.

- IDs SIAL iniciales de grado: 2 Economia, 3 Contador, 4 Administracion. Cursos fijos I3, I4, I5 e I6.
- Carrera 7 TGAA: solo I7. Carrera 8 TGE: solo I8.
- Modalidad 1: grupo P; modalidad 2: grupo D.
- Contabilidad se configura por modalidad y nombre de comision para todas las carreras de grado. Cada fila permite I1, I2 o Sin asignar. `COMISION 1` y `COMISION 01` son diferentes.
- Las reglas se guardan por campana. Cambiarlas invalida las vistas previas Moodle preparadas con reglas anteriores; no invalida las de Microsoft.
- Sin comision, sin destino configurado o con comisiones contradictorias, Contabilidad queda pendiente. Los cursos comunes pueden procesarse, y la tarea queda con incidencias, no completamente resuelta.
- Nunca asigna I1 e I2 simultaneamente, tampoco por combinar varias carreras. Si sus comisiones apuntan a ambos, retiene los cursos comunes y deja Contabilidad pendiente de revision.
- Si Moodle ya muestra una matricula en el otro curso de Contabilidad, no agrega el nuevo ni retira el anterior automaticamente: informa la diferencia para revisar. No elimina matriculas preexistentes en ambos cursos.
- Una carrera desconocida, modalidad desconocida o inscripciones contradictorias de la misma carrera bloquea esa persona en Moodle.
- Multiples carreras combinan cursos/grupos sin duplicar la matricula en un mismo curso. Comisiones TGE/TGAA adjuntas a una carrera de grado no se usan para elegir Contabilidad.
- Las matriculas de otros cursos se conservan; este modulo no ejecuta desmatriculaciones de cursos. En los cursos de destino solo puede retirar un grupo P/D previo que haya registrado como gestionado por Ingreso, despues de aplicar los destinos actuales. No elimina grupos personalizados.

## Procesos independientes

Microsoft: preparar consulta existencia para todas las personas de la campana. Al ejecutar en el futuro, vuelve a consultar; una cuenta existente no se recrea, no cambia de perfil/contrasena y no recibe correo de bienvenida. Una inexistencia confirmada permite crear, licenciar y luego enviar las credenciales mediante el SMTP y plantilla del CRM. Un error de consulta bloquea la fila. No llama a Moodle.

La contrasena temporal pendiente de envio se conserva cifrada con Fernet y una clave derivada de `CRM_SECRET_KEY`; se elimina del registro al confirmar el envio. Una licencia fallida puede reintentarse sin recrear la cuenta. Una creacion o entrega SMTP de resultado incierto requiere revision manual, no repeticion automatica. Un cambio del destinatario o del ID de cuenta tambien requiere revision. No se resetean contrasenas de cuentas existentes.

Moodle: preparar comprueba usuario, cursos, grupos y pertenencias. Al ejecutar vuelve a comprobarlo, crea el usuario Moodle si esta habilitada su creacion y falta, matricula/agrega grupos P/D faltantes sin repetir matriculas existentes. No consulta Microsoft ni envia correos. Acceder posteriormente mediante OpenID si requiere que exista la cuenta Microsoft.

Ambos procesos conservan vista previa, seleccion de filas/pagina/filtro, progreso y bitacora. Una sola tarea de Ingreso trabaja a la vez para evitar cruces entre operadores. No hay dependencia de exito entre Microsoft y Moodle. La pausa entre personas se configura con `INGRESO_STEP_PAUSE` (2 segundos por defecto); no sustituye las cuotas diarias del proveedor SMTP.

## Reinicio y pruebas

Una tarea interrumpida no ejecuta operaciones reales por reiniciar Windows. El bloqueo persistente vence tras 30 minutos sin progreso y se solicita preparar otra vista previa. Los estados de cuentas/licencias/correo y matriculas confirmadas se conservan para no repetir creaciones o envios.

`python -m unittest discover -s tests` prueba servicios simulados y bases temporales. `python tests/ingreso_browser_check.py` prueba la interfaz con Edge/Playwright, datos ficticios y servicios externos simulados. No crear cuentas ni matricular alumnos reales para verificar una instalacion.
