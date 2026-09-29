# Revisión adversarial de voz y retirada de RAG

Fecha: 2026-09-29. Rama: `codex/remove-resume-rag`. No se hizo push.

## Revisor y comparación

- Claude Code ejecutado directamente en terminal, siempre con `--model opus`.
- Rama base: `main`, commit `77514f716cc662c5a1cc526cba71ff7e5536d315`.
- Base elegida porque coincide con `origin/main` y es el padre del primer commit de esta rama.
- Comparación: `git diff main...HEAD`, incluyendo la retirada de RAG y las correcciones de voz.
- `session_id`: `260d1030-c698-412c-84f5-551f37701897`.
- Tres rondas de revisión en la misma sesión. La primera fue interrumpida por el cierre de la app y se reanudó sin cambiar de modelo ni iniciar otra sesión.
- Se leyeron los campos `result` de las tres respuestas JSON finales; todas terminaron con `subtype=success` e `is_error=false`.

| Ronda | Commit revisado | Resultado |
| --- | --- | --- |
| 1 | `211c831` | Seis observaciones: una alta, dos medias y tres bajas. |
| 2 | `686afea` | Sin problemas nuevos críticos, altos ni medios; cuatro observaciones bajas. |
| 3 | `459bc65` | Sin problemas críticos o altos confirmados; dos observaciones bajas y el riesgo STT aceptado pendiente de prueba real. |

## Evaluación propia y correcciones

| ID | Observación | Decisión y resultado |
| --- | --- | --- |
| 1 | STT puede conservar agregados cuando faltan tiempos, cambian los request IDs o no coinciden intervalos. | Descartado como defecto alto bajo la política elegida por el usuario: conservar frases ambiguas. Es un riesgo aceptado de duplicados, no evidencia de que el incidente del proveedor esté resuelto. Se añadieron advertencia acotada y diagnóstico sin texto del candidato. |
| 2 | CV y oferta completos sin límite. | Corregido: 30.000 caracteres de CV y 20.000 de oferta; rechazo HTTP 413 antes de planificar, repetir o emitir token. El constructor del prompt también valida. No se truncan documentos. |
| 3 | Volumen Qdrant antiguo fuera de la retención actual. | Corregida la documentación de migración en ambos README. La limpieza requiere actuación del administrador; no se borró el volumen conservado ni datos de Postgres. |
| 4 | Documentos sin delimitación dentro del prompt. | Mitigado con bloques XML escapados e instrucciones explícitas de tratar su contenido como datos. No se presenta como garantía total contra prompt injection. |
| 5 | Cancelar en `Reconnecting` trunca streams recuperables. | Corregido: cancelación al desconectarse participantes o la sala, conservando streams durante cortes breves. |
| 6 | Interrupciones posteriores a confirmar un turno no revierten herramientas. | Descartado como nueva regresión. Es un comportamiento previo; se aclaró el alcance de desactivar la generación especulativa en comentarios y README. |
| N1 | Diagnóstico incompleto para request IDs e intervalos distintos. | Corregido: logs DEBUG de ambos casos, sin contenido de voz. |
| N2 | Escape de apóstrofos/comillas introduce entidades y expande el prompt. | Corregido: `escape(..., quote=False)`; se siguen escapando los delimitadores XML. |
| N3 | El worker rechaza contexto y deja una sala sin entrevistador. | Corregido: transición condicionada a `error`, intento de cierre de la sala, liberación del engine y shutdown del job. |
| N4 | Un lector estancado queda pendiente indefinidamente. | Corregido: timeout de inactividad de 30 segundos, reiniciado por cada chunk. Conserva el parcial incompleto y no termina la entrevista. |
| R1 | Una desconexión puede llevar una fila en `error` a la pantalla de evaluación. | Confirmado al inspeccionar la interfaz y corregido después de la última ronda: `error` impide mostrar resultados, incluso con fase local `ended`. Prueba de regresión añadida. No se adoptó tratar todo `ROOM_DELETED` como error, porque el cierre normal también borra salas. |
| R2 | Los tests con mocks no demuestran reconexión del transporte real. | Descartado como bug del producto; es una limitación de las pruebas, registrada como pendiente de validación real. |

La corrección pequeña de R1 se validó localmente y no se envió a una cuarta ronda, respetando el máximo de tres solicitado.

## Verificación local

- 152 pruebas Python aprobadas, excluyendo la suite de LLM real.
- 25 pruebas del frontend aprobadas, incluidas las dos regresiones de enrutamiento de resultados posteriores a la tercera revisión.
- Ruff, formato Python, TypeScript, ESLint, Prettier y build aprobados.
- API `/api/healthz` y frontend responden HTTP 200; worker registrado en LiveKit.
- No se hicieron entrevistas reales ni llamadas a modelos de entrevista para estas pruebas. Claude Code sí se utilizó para la revisión solicitada.

## Pendientes y límites

El usuario aplazó la validación con voz real en Chrome y Safari. No se midió latencia acústica ni se verificaron tiempos/request IDs reales de AssemblyAI. Si el gateway no aporta evidencia suficiente, el filtro conserva la frase y pueden reaparecer duplicados; los logs permiten identificarlo. La prueba con fixtures históricas utiliza tiempos sintéticos y no demuestra el contrato del proveedor.

La retirada de RAG no borra automáticamente el Qdrant antiguo. Los README incluyen los pasos de limpieza específicos para ese despliegue.

La CLI no aplicó las reglas de Bash como una lista exclusiva: en la primera ejecución el revisor hizo pruebas locales y, en la tercera, reconoció una lectura `grep`/`sed` por Bash para inspeccionar el SDK. No editó archivos fuente ni cambió archivos versionados. Las invocaciones conservaron `--disallowedTools Edit Write`; no se creó ningún script de revisión ni archivo `.sh`.
