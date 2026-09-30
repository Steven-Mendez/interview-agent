# Interview Agent

[English](README.md) | **Español**

Un simulador de entrevistas de trabajo por voz con IA. Sube tu currículum (PDF) y pega una oferta de trabajo — un agente de IA planifica una entrevista a medida, la conduce contigo **por voz en el navegador** y te evalúa al terminar.

## Cómo funciona

Tres agentes, un solo flujo:

1. **Planner** — lee el currículum y la oferta, y diseña la entrevista (persona del entrevistador, hitos a cubrir) en el idioma que configuraste.
2. **Interviewer** — un agente de voz en tiempo real que conduce la entrevista en el navegador sobre LiveKit, va marcando los hitos y usa el currículum completo y la oferta en su contexto para fundamentar las preguntas.
3. **Evaluator** — cuando la entrevista termina (plan completado, límite de tiempo, o cierras la pestaña), evalúa el transcript automáticamente: contratado o no, puntuación, fortalezas y debilidades.

## Stack

- **LiveKit Agents** — pipeline de audio en tiempo real (STT, TTS, detección de turnos)
- **LangGraph** — el cerebro del entrevistador (grafo ReAct + tools)
- **OpenAI** — LLMs para planificación, entrevista y evaluación
- **PostgreSQL** — conversaciones, hitos, transcripts, evaluaciones
- **FastAPI** — el API, bajo `/api` (también sirve el frontend compilado)
- **TanStack Start + shadcn/ui** — frontend React en `web/`, compilado como SPA

## Requisitos

- Docker
- Una API key de OpenAI
- Un proyecto gratuito de LiveKit Cloud (URL + API key + secret) — [cloud.livekit.io](https://cloud.livekit.io)

## Cómo correrlo

```bash
cp .env.example .env         # completa tus claves
docker compose up -d --build
```

Eso levanta todo el stack: Postgres, la API + frontend y el worker de LiveKit (las migraciones corren automáticamente). El currículum se guarda como texto en Postgres; los tres agentes lo reciben directamente, sin embeddings ni base de datos vectorial.

Abre <http://localhost:8000>: sube un currículum en PDF, pega la oferta de trabajo, espera el plan (~30–60 s) y empieza la entrevista por voz. Al terminar, la evaluación aparece en la misma página.

La pantalla **History** lista todas las entrevistas que corriste, de la más reciente a la más antigua, con su puntaje, su veredicto y hasta dónde llegaron los temas — y se puede filtrar por estado. Al abrir una ves su scorecard y, plegada, la transcripción completa. **Repeat** vuelve a correr el mismo puesto: una entrevista nueva a partir del currículum y la oferta ya guardados, al mismo nivel y duración, con preguntas planificadas de cero. La original no se toca, y cada repetición queda agrupada bajo ella.

La pantalla **Settings** configura el agente de forma global: su nombre, el idioma de la entrevista (inglés o español, con una voz femenina y una masculina por idioma), una persona opcional para el entrevistador e instrucciones custom. Los cambios aplican a las entrevistas creadas después.

## Contexto de voz y recuperación

El texto extraído del currículum admite hasta 30.000 caracteres y la oferta hasta 20.000. Los documentos mayores reciben HTTP 413 antes de planificar o iniciar la sesión de voz, incluidas las entrevistas guardadas antes de estos límites. El contenido no se trunca silenciosamente. Estos límites acotan el contexto completo para el uso interactivo por voz; la latencia también depende del modelo y de la longitud de la conversación.

La generación especulativa está desactivada porque las herramientas de LangGraph guardan hitos y activan el cierre. Interrumpir una respuesta ya confirmada no revierte las herramientas ejecutadas. Los finales duplicados de AssemblyAI se filtran solo cuando su texto normalizado y sus intervalos de audio identifican voz ya recibida. Si faltan tiempos válidos, se conserva la frase y el worker registra una advertencia por stream STT. Las fixtures históricas de texto usan tiempos sintéticos en las pruebas; quedan pendientes los metadatos reales del proveedor y la validación con micrófono.

Los streams de transcripción fallidos o abandonados conservan el texto recibido con la marca **Incomplete transcription**. Una reconexión breve puede continuar el stream existente; un lector sin datos durante 30 segundos se cancela y queda marcado como incompleto sin terminar la entrevista. El control **Enable audio** aparece cuando el navegador bloquea la reproducción.

Las burbujas del usuario siguen los turnos de conversación, en vez de las frases individuales de STT. El worker publica texto acumulado por `interview.user_transcription` con un mismo identificador hasta que `conversation_item_added` confirma la respuesta exacta. Se conservan las frases repetidas; los turnos confirmados distintos mantienen identificadores diferentes. La voz del agente sigue usando el stream sincronizado `lk.transcription` de LiveKit. Actualiza worker y frontend juntos para este protocolo.

Si el SDK no confirma un turno del usuario, el texto presente antes de una respuesta sin interrupciones del agente se conserva como incompleto y la siguiente respuesta recibe otro identificador. Esto requiere que no haya nueva voz ni transcripción del usuario durante esa respuesta. Las interrupciones, los finales tardíos de STT y los límites ambiguos conservan el identificador hasta confirmar el turno del usuario. Los envíos finales fallidos se reintentan una vez con el mismo identificador; si el transporte sigue fallando, el parcial queda incompleto y el texto confirmado permanece en Postgres. El cierre del publicador espera como máximo 10,1 segundos.

La espera mínima de cierre de turno es de 1,5 segundos; la espera para dudas o pausas más largas sigue en 2,5 segundos. Esto cubre, con margen, el final de STT que llegó 1,17 segundos tarde en la prueba de voz del 30 de septiembre, a cambio de hasta 1,2 segundos más de espera que el mínimo anterior. Las regresiones locales con el SDK reproducen la división anterior y verifican que ese fragmento queda en un solo turno. Los retrasos mayores del proveedor pueden requerir más mediciones; quedan pendientes las pruebas con micrófono en Chrome y Safari posteriores al cambio.

## Actualización desde la versión con Qdrant

Retirar Qdrant de Compose no elimina sus contenedores ni el volumen `qdrant_data` existente. La tarea actual de retención solo gestiona Postgres; los datos vectoriales antiguos requieren una limpieza única por parte del administrador del despliegue.

Identifica el contenedor y el volumen de Qdrant de este despliegue con `docker ps -a` y `docker volume ls` (las etiquetas de Compose indican el proyecto, servicio y volumen). Si ya no necesitas los datos vectoriales antiguos, elimina únicamente esos recursos de Qdrant:

```bash
docker rm -f <contenedor-qdrant-antiguo>
docker volume rm <volumen-qdrant-antiguo>
```

Conserva el contenedor y el volumen de Postgres: guardan los currículums, entrevistas y evaluaciones de esta versión. No uses una limpieza general de volúmenes. Esta actualización no borra automáticamente los datos antiguos de Qdrant.

## Desarrollo (local)

Para iterar con hot reload, corre solo Postgres en Docker y la app con [uv](https://docs.astral.sh/uv/) (Python 3.12+) y [pnpm](https://pnpm.io/) (Node 22+):

```bash
uv sync
docker compose up -d postgres           # Postgres (:5432)
uv run alembic upgrade head             # crea el esquema

# Terminal 1: el worker de LiveKit (el entrevistador)
uv run python main.py dev

# Terminal 2: la API
uv run uvicorn interview_agent.server.app:app --port 8000

# Terminal 3: el dev server del frontend (HMR, hace proxy de /api a :8000)
cd web && pnpm install && pnpm dev
```

Abre <http://localhost:3000> para el frontend de desarrollo. (El uvicorn en :8000 sirve el último `pnpm build`, si existe — el comportamiento de producción.)

> **Nota sobre el idioma y la voz:** el idioma de la entrevista, el nombre del agente y su voz se configuran en la pantalla Settings de la app (no en el `.env`). El reconocimiento y la síntesis de voz quedan fijados al idioma configurado; el catálogo de voces vive en `interview_agent/voices.py`.
