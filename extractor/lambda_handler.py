"""AWS Lambda entrypoint: mangum(FastAPI app)."""
from extractor.api import handler as handler  # noqa: F401
