# Interview Agent

[English](README.md) | **Español**

Un simulador de entrevistas de trabajo por voz con IA. Sube tu currículum (PDF) y pega una oferta de trabajo — un agente de IA planifica una entrevista a medida, la conduce contigo **por voz en el navegador** y te evalúa al terminar.

## Cómo funciona

Tres agentes, un solo flujo:

1. **Planner** — lee el currículum y la oferta, y diseña la entrevista (persona del entrevistador, hitos a cubrir) en el idioma que configuraste.
2. **Interviewer** — un agente de voz en tiempo real que conduce la entrevista en el navegador sobre LiveKit, va marcando los hitos y puede buscar en tu currículum durante la conversación (RAG).
3. **Evaluator** — cuando la entrevista termina (plan completado, límite de tiempo, o cierras la pestaña), evalúa el transcript automáticamente: contratado o no, puntuación, fortalezas y debilidades.

## Stack

- **LiveKit Agents** — pipeline de audio en tiempo real (STT, TTS, detección de turnos)
- **LangGraph** — el cerebro del entrevistador (grafo ReAct + tools)
- **OpenAI** — LLMs y embeddings
- **Qdrant** — búsqueda vectorial sobre el currículum
- **PostgreSQL** — conversaciones, hitos, transcripts, evaluaciones
- **FastAPI** — API + frontend en HTML/CSS/JS plano

## Requisitos

- Docker
- Una API key de OpenAI
- Un proyecto gratuito de LiveKit Cloud (URL + API key + secret) — [cloud.livekit.io](https://cloud.livekit.io)

## Cómo correrlo

```bash
cp .env.example .env         # completa tus claves
docker compose up -d --build
```

Eso levanta todo el stack: Postgres, Qdrant, la API + frontend y el worker de LiveKit (las migraciones corren automáticamente).

Abre <http://localhost:8000>: sube un currículum en PDF, pega la oferta de trabajo, espera el plan (~30–60 s) y empieza la entrevista por voz. Al terminar, la evaluación aparece en la misma página.

La pantalla **Settings** configura el agente de forma global: su nombre, el idioma de la entrevista (inglés o español, con una voz femenina y una masculina por idioma), una persona opcional para el entrevistador e instrucciones custom. Los cambios aplican a las entrevistas creadas después.

## Desarrollo (local)

Para iterar con hot reload, corre solo las bases de datos en Docker y la app con [uv](https://docs.astral.sh/uv/) (Python 3.12+):

```bash
uv sync
docker compose up -d postgres qdrant    # Postgres (:5432) + Qdrant (:6333)
uv run alembic upgrade head             # crea el esquema

# Terminal 1: el worker de LiveKit (el entrevistador)
uv run python main.py dev

# Terminal 2: la API + frontend
uv run uvicorn interview_agent.server.app:app --port 8000
```

> **Nota sobre el idioma y la voz:** el idioma de la entrevista, el nombre del agente y su voz se configuran en la pantalla Settings de la app (no en el `.env`). El reconocimiento y la síntesis de voz quedan fijados al idioma configurado; el catálogo de voces vive en `interview_agent/voices.py`.
