# gateway/main.py
from fastapi import FastAPI
from gateway.endpoints.openai_compat import router as openai_router
import logging

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="AI Continuity Gateway")

app.include_router(openai_router)

@app.get("/")
def health_check():
    return {"status": "healthy"}