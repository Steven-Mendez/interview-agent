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

## Seguimiento: burbujas por turno y finales tardíos (2026-09-30)

### Evidencia de las dos entrevistas de prueba

Se contrastaron los logs del worker y las transcripciones guardadas en Postgres:

- Entrevista reanudada `23cdc023-a58d-40f5-9d9d-098d56c5628a`: en el tramo reanudado hubo 11 finales STT y cinco mensajes del usuario confirmados. La respuesta de JavaScript tuvo tres segmentos STT y un único mensaje guardado; las burbujas separadas eran un efecto de mostrar cada segmento. La respuesta de Git sí quedó dividida en cuatro turnos guardados. Hubo un final STT a 1,17 segundos del fin de audio, 138 ms después del commit anterior, junto al aviso del SDK de final tardío.
- Entrevista nueva `af813f03-3af7-4703-9b3e-3e6ba958c890`: un único job sin reanudar, cinco finales STT y cuatro respuestas confirmadas. La presentación tuvo dos segmentos y un mensaje guardado. No apareció el aviso de final tardío; se completaron los cuatro hitos y la evaluación.

No se demuestra que la reconexión causara el final tardío. La separación visual por segmentos STT existe independientemente de reconectar. Estos datos tampoco explican el cierre de la aplicación ChatGPT.

### Corrección

El worker publica el texto del usuario por `interview.user_transcription`, acumulado bajo un identificador por turno hasta el evento público `conversation_item_added`. El frontend usa ese stream para el usuario y conserva el stream sincronizado oficial para la voz del agente. No se eliminan repeticiones por texto.

La espera mínima de endpointing pasa de 0,3 a 1,5 segundos; el máximo sigue en 2,5. La evidencia anterior justifica este margen para el final observado, a cambio de hasta 1,2 segundos adicionales de espera. No garantiza cubrir retrasos mayores ni sustituye medir la latencia acústica con micrófono.

### Nueva revisión de Opus

- Base elegida y verificada otra vez: `main`, merge-base `77514f716cc662c5a1cc526cba71ff7e5536d315`.
- Nueva sesión para estos cambios: `f302e7fc-320d-4af2-8599-703a004f9587`.
- Tres invocaciones directas de Claude Code, todas con `--model opus`, diff `main...HEAD`, sin scripts ni archivos `.sh`.
- Invocación 1 sobre `ac406b9`: `error_max_turns`, sin campo `result`; alcanzó el límite de 15 turnos, sin fallo de red, autenticación o permisos. Se guardó el `session_id` y se reanudó esa misma sesión.
- Invocación 2 sobre `ac406b9`: `success`; se leyó `result` completo. Un hallazgo crítico y tres bajos.
- Invocación 3 sobre `33c0d58`: `success`; se leyó `result` completo. Confirmó las correcciones de arranque y reintento, aceptó el temporizador previsto y detectó una regresión media al interrumpir.

| Hallazgo | Decisión y validación |
| --- | --- |
| Crítico: acceder a `room.local_participant` antes de conectar impide arrancar todas las entrevistas. | Corregido: se pasa la sala y se resuelve el participante al publicar. Pruebas con sala RTC real sin conectar y con la ruta del worker hasta `session.start`. Opus confirmó la corrección. |
| Bajo: un turno sin `conversation_item_added` contamina la respuesta siguiente. | Corregido con conservación del texto incompleto y rotación del id solo ante un límite de voz inequívoco. Se conserva el id si hay voz nueva, STT tardío o ambigüedad. |
| Bajo: un envío final fallido no tiene un snapshot posterior que lo recupere. | Corregido: un reintento acotado bajo el mismo id. El frontend protege el final confirmado y descarta lectores antiguos. Opus confirmó la corrección. Un fallo persistente todavía puede dejar el parcial incompleto; el texto confirmado sigue en Postgres. |
| Bajo: la marca de incompleto puede aparecer mientras todavía se calcula el final. | No se cambia: el plan aprobado exige el temporizador de tres segundos sin final. El final posterior corrige la marca; no hay pérdida de texto. Opus aceptó esta decisión. |
| Medio N1: el mensaje del asistente interrumpido llega antes del commit del usuario y la rotación puede duplicar su respuesta. | Corregido después de la tercera ronda: no rotar por mensajes interrumpidos; exigir texto anterior al inicio de voz del agente, sin nueva voz ni STT del usuario durante esa respuesta. Pruebas de interrupción, continuación posterior, STT tardío, voz antes del texto y ausencia de límite inequívoco. |

No se hizo una cuarta invocación. La última corrección de N1 se verificó localmente, pero Opus no revisó esa versión posterior. No quedan hallazgos críticos o altos conocidos sin corregir.

### Verificación actual y límites

- 170 pruebas Python aprobadas, excluyendo las de LLM real; incluye reproducción con el SDK de los finales a 1,17 segundos y comparación de esperas de 0,3 y 1,5 segundos, con dobles locales.
- 28 pruebas del frontend aprobadas; TypeScript, ESLint, Prettier, build, Ruff y formato Python aprobados.
- Transporte LiveKit real con dos clientes RTC y una sala temporal: seis snapshots, un id para varias frases, repetición conservada, huérfano incompleto y respuesta interrumpida confirmada sin duplicación. Sala eliminada al terminar; sin llamadas a LLM, STT ni TTS para esta verificación.
- Las dos entrevistas de voz anteriores al cambio sí usaron los proveedores configurados. No se presenta la prueba de texto RTC como una prueba de captura del micrófono, reproducción de audio o reconexión real.
- La comprobación posterior al cambio con micrófono en Chrome y Safari sigue pendiente, conforme a lo acordado con el usuario.
- Cambios locales sobre `codex/remove-resume-rag`, sin push.
