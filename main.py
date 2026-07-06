"""Entry point for the interview agent LiveKit worker.

Usage:
    uv run python main.py dev      # worker with hot reload (local dev)
    uv run python main.py start    # worker in production mode (Docker)
"""

from interview_agent.agent import run
from interview_agent.logging_config import setup_file_logging


def main() -> None:
    log_path = setup_file_logging()
    print(f"[interview-agent] Writing logs to {log_path.resolve()}")
    run()


if __name__ == "__main__":
    main()
