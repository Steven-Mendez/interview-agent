"""AI interview agent: LiveKit Agents + LangGraph + OpenAI."""

__all__ = ["entrypoint"]


def __getattr__(name):
    # Importing planning/evaluation must not load the voice worker or its SDK.
    if name == "entrypoint":
        from interview_agent.agent import entrypoint

        return entrypoint
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
