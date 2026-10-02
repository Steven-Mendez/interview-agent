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

Abre <http://localhost:8000> y elige **New interview**: sube un currículum en PDF, pega la oferta de trabajo y espera el plan (~30–60 s). La sala de preparación te permite revisar el micrófono y la cámara antes de empezar la entrevista por voz. Al terminar, los resultados se abren en la aplicación en cuanto la evaluación está lista.

La pantalla **History** lista todas las entrevistas que corriste, de la más reciente a la más antigua, con su puntaje, su veredicto y hasta dónde llegaron los temas — y se puede filtrar por estado. Al abrir una ves su scorecard y, plegada, la transcripción completa. **Repeat** vuelve a correr el mismo puesto: una entrevista nueva a partir del currículum y la oferta ya guardados, al mismo nivel y duración, con preguntas planificadas de cero. La original no se toca, y cada repetición queda agrupada bajo ella.

La pantalla **Settings** configura el agente de forma global: su nombre, el idioma de la entrevista (inglés o español, con una voz femenina y una masculina por idioma), una persona opcional para el entrevistador e instrucciones custom. Los cambios aplican a las entrevistas creadas después.

## Contexto de voz y recuperación

El texto extraído del currículum admite hasta 30.000 caracteres y la oferta hasta 20.000. Los documentos mayores reciben HTTP 413 antes de planificar o iniciar la sesión de voz. El contenido no se trunca silenciosamente. Revisa el texto extraído del PDF antes de enviarlo; el texto revisado queda vinculado al hash del PDF subido.

Cada entrevista guarda su nivel, idioma, voz, modelos, límites de preguntas y duración efectivos. Reconectar conserva el inicio original y el presupuesto consumido. El contador visible usa el tiempo calculado por PostgreSQL y un intervalo monotónico local entre actualizaciones; si el dato no está disponible, muestra `--:--`.

Las respuestas del candidato tienen identidades lógicas estables y versiones inmutables de texto. El texto confirmado y su procedencia se guardan antes de utilizarse en decisiones. Las correcciones y capturas tardías o conflictivas permanecen identificables; repetir las mismas palabras no demuestra duplicación. Las transcripciones sin confirmar se conservan como incompletas. Las decisiones especulativas están desactivadas y las decisiones validadas se guardan antes de entregar la pregunta.

Una pregunta guardada que nunca empezó puede recuperarse con el mismo ID. Una entrega incierta o interrumpida no se repite automáticamente. **Listen again** (dentro de **Current question**) solicita expresamente la pregunta guardada sin gastar otra pregunta ni decisión de modelo, siempre que una respuesta ya confirmada no impida repetirla.

En el cierre normal, el navegador reproduce la despedida localizada y envía una confirmación durable. El worker drena el reconocimiento y guarda un sello inmutable del transcript antes de habilitar los resultados. Un timeout o una confirmación ausente no se presenta como reproducción correcta. **Enable audio** aparece si el navegador bloquea el audio. La pérdida del worker, el abandono y la integridad incompleta siguen siendo visibles; el audio perdido no cuenta como una respuesta incorrecta.

Los resultados separan cobertura de capacidad y muestran evidencia por criterio y práctica sugerida. **Request new assessment** crea una solicitud distinta conservando el feedback anterior; si se pierde la respuesta HTTP, el reintento mantiene la misma identidad. **Evaluation history** incluye resultados anteriores e intentos fallidos. **Review saved answers** permite decidir incidentes expresamente y crear nuevas versiones del transcript; conserva versiones y evaluaciones previas, y una omisión confirmada mantiene oculta la puntuación global antigua afectada. Estos controles son opcionales y no exigen revisión manual para realizar una entrevista.

Las esperas actuales de cierre de turno son 1,5 segundos como mínimo y 2,5 segundos para dudas más largas. Las pruebas SDK y de audio sintético cubren los contratos implementados; no acreditan comportamiento del micrófono físico, latencia audible ni mejora de calidad de entrevista. La latencia, el gasto y los resultados de cierre se exportan como métricas anónimas de OpenTelemetry (solo categorías y números) a Grafana: el servicio `lgtm` de compose lo sirve en <http://localhost:3001> con los dashboards de `infra/grafana/dashboards`. Los logs locales nunca contienen el contenido de las entrevistas.

LangSmith es opcional, y configurar `LANGSMITH_API_KEY` es el consentimiento para enviarle todo el **contenido** de cada entrevista: CV, oferta, plan, turnos, prompts, respuestas del modelo, evaluación y el audio de la sesión. Sin la key no se envía nada. Con ella, la app usa el trazado nativo de LangSmith, y las trazas de cada entrevista se agrupan en un mismo hilo (thread) de LangSmith:

- **`planner`**: entran el CV, la oferta y las opciones de la entrevista; sale el plan.
- **`dialogue_turn`**, una por turno: el grafo de decisión y la llamada al modelo con su prompt, respuesta y tokens.
- **La sesión de voz**, una por cada ejecución del worker, de la integración oficial de LangSmith con LiveKit: su raíz lleva la **grabación de audio estéreo** (candidato y entrevistador) y la conversación, y debajo cada turno con sus latencias de STT/LLM/TTS.
- **`evaluator`**: entran el CV, la oferta, el plan y la transcripción; salen nota, evidencia y comentarios.

Para encontrar una entrevista, abre la pestaña **Threads** del proyecto y filtra por `thread_id` (el ID de la entrevista original, compartido por sus repeticiones) o por el metadato `interview_id`. LangSmith solo muestra el coste de los modelos con precio: en **Settings > Model Pricing**, añade filas para `gpt-6-astra`, `gpt-6.1-sol`, `gpt-5.5` y `gpt-5.4-mini`, incluido el precio de los tokens en caché. Grafana sigue siendo la fuente de verdad del gasto.

Después, el contenido sigue la retención propia del proyecto de LangSmith (base 14 días, extendida 400; lo que se añada a un dataset o experimento puede durar más), y la app nunca lo borra. Con un proyecto de LiveKit Cloud que tenga activada la observabilidad de agentes, LiveKit Cloud también recibe la grabación, con la retención propia de LiveKit.

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

Para desarrollo local define `AUTH_MODE=local` en el `.env` (`.env.example` ya lo hace; el stack de Docker Compose lo usa por defecto): no hay login y cada petición es el usuario `local-dev`, admin si figura en `ADMIN_USER_IDS`. Para probar el login en local (la web, compilada sin `VITE_NEON_AUTH_URL`, consulta `GET /api/auth/config` y muestra un formulario de usuario y contraseña), define cuentas en `LOCAL_ACCOUNTS` (`admin:<clave>,guest:<clave>`, con ids `local:<usuario>`, p. ej. `ADMIN_USER_IDS=local-dev,local:admin`) y `INTERNAL_API_TOKEN`: la API firma entonces sus propios tokens y cada ruta de usuario exige uno. El navegador guarda ese token en `localStorage`, aceptable solo porque este modo nunca se ejecuta fuera de desarrollo: con cualquier `AUTH_MODE=local` la API se niega a arrancar si `DATABASE_URL` apunta a un host remoto o si `SENTRY_ENVIRONMENT=production`. El modo por defecto, `AUTH_MODE=neon`, verifica los tokens de Neon Auth e ignora `LOCAL_ACCOUNTS`. Las entrevistas creadas antes de que existieran las cuentas no tienen dueño y quedan ocultas hasta que las asignes: `uv run python scripts/claim_interviews.py --owner local-dev`. El perfil de cada usuario guarda el email y el nombre que trae su inicio de sesión, y cuándo se le vio por última vez, fuera de la purga de retención de entrevistas (los admins listan los usuarios en `GET /api/admin/users`, en la web en `/admin/users`; cada uno ve el suyo en `/profile`); el dashboard de Grafana **Interview Agent users** solo muestra recuentos (usuarios, actividad, cuotas, inicios de sesión), nunca quién.

Quién es admin lo decide solo `ADMIN_USER_IDS`: una lista separada por comas de ids de usuario (el `sub` del JWT; `local:<usuario>` para una cuenta local) que la API lee al arrancar. Los admins no tienen cupo de entrevistas y ven la lista de usuarios; el resto son invitados, con `LIFETIME_INTERVIEWS_PER_USER` entrevistas cada uno y `GUEST_INTERVIEWS_PER_MONTH` entre todos. Nada en el token ni en Neon Auth otorga el rol, así que una cuenta no puede ascenderse sola. Para añadir un admin: entra una vez, lees su id en `/admin/users` (o en `user_profiles`), lo añades a la variable y reinicias la API. Un usuario de Neon Auth tiene el mismo id en todos los sitios donde se use el mismo proyecto de Neon Auth, así que el id que aparece en desarrollo es el que va en producción.

Para un despliegue: la base Postgres en Neon (con Neon Auth) y los dashboards de Grafana Cloud se aprovisionan con Terraform en `infra/` — [`infra/README.md`](infra/README.md) tiene los pasos, y de dónde sale `NEON_AUTH_URL`. El frontend se compila con sus propias variables, listadas en `web/.env.example`: `VITE_NEON_AUTH_URL` para el inicio de sesión con Neon Auth, `VITE_API_BASE_URL` cuando la API vive en otro origen, `VITE_SENTRY_DSN` para los errores del navegador. En el servidor, `.env.example` documenta los opcionales `SENTRY_DSN` y `SENTRY_ENVIRONMENT` (errores sin contenido de entrevistas), `CORS_ALLOWED_ORIGINS` (cuando la web se sirve desde otro origen) y los `OTEL_EXPORTER_OTLP_*` para Grafana Cloud.

Los tests solo necesitan Docker en marcha: `uv run pytest` levanta un Postgres 16 desechable para la sesión y lo elimina al terminar (sin `.env` ni base de datos levantada). Para usar una base existente, define `TEST_DATABASE_URL` con una cuyo nombre termine en `_test` (CI lo hace con su contenedor de servicio).

> **Nota sobre el idioma y la voz:** el idioma de la entrevista, el nombre del agente y su voz se configuran en la pantalla Settings de la app (no en el `.env`). El reconocimiento y la síntesis de voz quedan fijados al idioma configurado; el catálogo de voces vive en `interview_agent/voices.py`.
