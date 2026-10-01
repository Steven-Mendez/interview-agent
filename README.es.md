# Interview Agent

[English](README.md) | **Español**

Un simulador de entrevistas de trabajo por voz con IA. Sube tu currículum (PDF) y pega una oferta de trabajo — un agente de IA planifica una entrevista a medida, la conduce contigo **por voz en el navegador** y te evalúa al terminar.

## Cómo funciona

Tres agentes, un solo flujo:

1. **Planner** — lee el currículum y la oferta, y diseña la entrevista (persona del entrevistador, hitos a cubrir) en el idioma que configuraste.
2. **Interviewer** — un agente de voz en tiempo real que conduce la entrevista en el navegador sobre LiveKit, va marcando los hitos y usa el currículum completo y la oferta en su contexto para fundamentar las preguntas.
3. **Evaluator** — tras el cierre y un sello inmutable del transcript, evalúa cada criterio con evidencia del candidato. Las entrevistas parciales o insuficientes no reciben puntuación ni veredicto global. Las solicitudes recuperables conservan sus intentos y resultados anteriores.

## Stack

- **LiveKit Agents** — pipeline de audio en tiempo real (STT, TTS, detección de turnos)
- **LangGraph** — decisiones de entrevista validadas y persistidas transaccionalmente
- **OpenAI** — LLMs para planificación, entrevista y evaluación
- **PostgreSQL** — conversaciones, hitos, transcripts, evaluaciones
- **FastAPI** — el API, bajo `/api` (también sirve el frontend compilado)
- **TanStack Start + shadcn/ui** — frontend React en `web/`, compilado como SPA

## Requisitos

- Docker
- Una API key de OpenAI
- Un proyecto de LiveKit Cloud (URL + API key + secret) — [cloud.livekit.io](https://cloud.livekit.io)

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

El texto extraído del currículum admite hasta 30.000 caracteres y la oferta hasta 20.000. Los documentos mayores reciben HTTP 413 antes de planificar o iniciar la sesión de voz. El contenido no se trunca silenciosamente. Revisa el texto extraído del PDF antes de enviarlo; el texto revisado queda vinculado al hash del PDF subido.

Cada entrevista guarda su nivel, idioma, voz, modelos, límites de preguntas y duración efectivos. Reconectar conserva el inicio original y el presupuesto consumido. El contador visible usa el tiempo calculado por PostgreSQL y un intervalo monotónico local entre actualizaciones; si el dato no está disponible, muestra `--:--`.

Las respuestas del candidato tienen identidades lógicas estables y versiones inmutables de texto. El texto confirmado y su procedencia se guardan antes de utilizarse en decisiones. Las correcciones y capturas tardías o conflictivas permanecen identificables; repetir las mismas palabras no demuestra duplicación. Las transcripciones sin confirmar se conservan como incompletas. Las decisiones especulativas están desactivadas y las decisiones validadas se guardan antes de entregar la pregunta.

Una pregunta guardada que nunca empezó puede recuperarse con el mismo ID. Una entrega incierta o interrumpida no se repite automáticamente. **Listen again** (dentro de **Current question**) solicita expresamente la pregunta guardada sin gastar otra pregunta ni decisión de modelo, siempre que una respuesta ya confirmada no impida repetirla.

En el cierre normal, el navegador reproduce la despedida localizada y envía una confirmación durable. El worker drena el reconocimiento y guarda un sello inmutable del transcript antes de habilitar los resultados. Un timeout o una confirmación ausente no se presenta como reproducción correcta. **Enable audio** aparece si el navegador bloquea el audio. La pérdida del worker, el abandono y la integridad incompleta siguen siendo visibles; el audio perdido no cuenta como una respuesta incorrecta.

Los resultados separan cobertura de capacidad y muestran evidencia por criterio y práctica sugerida. **Request new assessment** crea una solicitud distinta conservando el feedback anterior; si se pierde la respuesta HTTP, el reintento mantiene la misma identidad. **Evaluation history** incluye resultados anteriores e intentos fallidos. **Review saved answers** permite decidir incidentes expresamente y crear nuevas versiones del transcript; conserva versiones y evaluaciones previas, y una omisión confirmada mantiene oculta la puntuación global antigua afectada. Estos controles son opcionales y no exigen revisión manual para realizar una entrevista.

Las esperas actuales de cierre de turno son 1,5 segundos como mínimo y 2,5 segundos para dudas más largas. Las pruebas SDK y de audio sintético cubren los contratos implementados; no acreditan comportamiento del micrófono físico, latencia audible ni mejora de calidad de entrevista. La página **Metrics** muestra latencia, gasto y resultados de cierre registrados solo como metadatos; con `LANGSMITH_API_KEY` se exportan además trazas sin contenido.

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
