"""FastAPI entrypoint discovered from the CodeRepository's top-level api directory."""

from fastapi import FastAPI
from mainsequence.client import User
from mainsequence.server.fastapi import install_request_identity

app = FastAPI()
install_request_identity(app)


@app.get("/")
async def hello() -> dict[str, str]:
    return {"message": "Hello, world!", "user_uid": User.get_logged_user().uid}
