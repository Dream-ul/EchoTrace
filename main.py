"""Local EchoTrace server entry point.

The repository-standard ``.env`` is loaded here, before the FastAPI composition
root imports its runtime. Deployment entrypoints inject environment variables
directly and therefore keep precedence over this convenience loader.
"""

import os

from echotrace.runtime.environment import load_local_environment


load_local_environment()
os.environ.setdefault("ECHOTRACE_PROCESS", "local")

import uvicorn

from app.main import app


if __name__ == "__main__":
    uvicorn.run(app, host=os.getenv("ECHOTRACE_HOST", "0.0.0.0"), port=int(os.getenv("ECHOTRACE_PORT", "8000")))
