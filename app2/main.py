"""
App2 - Inventory Service.

Python/FastAPI counterpart to App1. Deliberately exposes the SAME endpoint
contract (/health, /api/info, /api/items) so the platform validates both
identically - the deployment pipeline does not care which language it just
shipped.
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# --------------------------------------------------------------------------
# Config comes entirely from the environment, which Terraform populates. This
# is how "deploy app2 for acme" becomes a service that knows it is acme's.
# --------------------------------------------------------------------------
PORT = int(os.getenv("PORT", "8080"))
CUSTOMER = os.getenv("CUSTOMER", "unknown")
ENVIRONMENT = os.getenv("ENVIRONMENT", "unknown")
APP_NAME = os.getenv("APP_NAME", "app2")
PROJECT = os.getenv("GCP_PROJECT", "")
REVISION = os.getenv("K_REVISION", "local")
STARTED_AT = datetime.now(timezone.utc).isoformat()

COLLECTION = f"{APP_NAME}-{CUSTOMER}-{ENVIRONMENT}-stock"


# --------------------------------------------------------------------------
# Structured logs, matching App1. Cloud Run's log viewer parses a JSON line
# into fields, so "severity" and "message" become filterable. Previously this
# service ran with uvicorn's log config disabled, which left it silent - a
# deployment you cannot debug looks identical to one that is merely quiet.
# --------------------------------------------------------------------------
def log(message: str, severity: str = "INFO", **fields) -> None:
    sys.stdout.write(
        json.dumps({
            "severity": severity,
            "message": message,
            "app": APP_NAME,
            "customer": CUSTOMER,
            "environment": ENVIRONMENT,
            "revision": REVISION,
            **fields,
        }) + "\n"
    )
    sys.stdout.flush()


class JsonFormatter(logging.Formatter):
    """Wraps uvicorn's own records in the same shape."""

    def format(self, record: logging.LogRecord) -> str:
        return json.dumps({
            "severity": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
            "app": APP_NAME,
            "customer": CUSTOMER,
            "environment": ENVIRONMENT,
            "revision": REVISION,
        })

# Lazy: /health must answer even if Firestore is unreachable, otherwise a
# transient database problem looks like a failed deployment.
_db = None
_db_error: Optional[str] = None


def db():
    global _db, _db_error
    if _db is None and _db_error is None:
        try:
            from google.cloud import firestore

            _db = firestore.Client(project=PROJECT) if PROJECT else firestore.Client()
        except Exception as err:  # noqa: BLE001
            _db_error = str(err)
            log("firestore client unavailable", "ERROR", detail=_db_error)
    return _db


app = FastAPI(title="Inventory Service", docs_url="/docs", redoc_url=None)


class StockItem(BaseModel):
    sku: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=120)
    quantity: int = Field(default=0, ge=0, le=1_000_000)


@app.get("/health")
def health():
    """Deliberately does not touch Firestore - see App1 for the reasoning."""
    return {
        "status": "ok",
        "app": APP_NAME,
        "revision": REVISION,
    }


@app.get("/api/info")
def info():
    """
    The validation step asserts customer and environment here. This is what
    proves the deployment was parameterised rather than shipped fixed.
    """
    return {
        "app": APP_NAME,
        "customer": CUSTOMER,
        "environment": ENVIRONMENT,
        "cloud": "gcp",
        "platform": "cloud-run",
        "runtime": f"python {sys.version_info.major}.{sys.version_info.minor}",
        "dataStore": "firestore",
        "region": os.getenv("GCP_REGION", "us-central1"),
        "revision": REVISION,
        "startedAt": STARTED_AT,
        "deployedBy": "agentic-deploy-poc",
    }


@app.get("/api/items")
def list_items():
    client = db()
    if client is None:
        raise HTTPException(503, f"data store unavailable: {_db_error}")
    try:
        docs = (
            client.collection(COLLECTION)
            .order_by("createdAt", direction="DESCENDING")
            .limit(50)
            .stream()
        )
        items = [{"id": d.id, **d.to_dict()} for d in docs]
        return {"collection": COLLECTION, "count": len(items), "items": items}
    except Exception as err:  # noqa: BLE001
        raise HTTPException(500, f"query failed: {err}") from err


@app.post("/api/items", status_code=201)
def add_item(item: StockItem):
    client = db()
    if client is None:
        raise HTTPException(503, f"data store unavailable: {_db_error}")
    try:
        doc = {
            **item.model_dump(),
            "customer": CUSTOMER,
            "environment": ENVIRONMENT,
            "createdAt": datetime.now(timezone.utc).isoformat(),
        }
        ref = client.collection(COLLECTION).add(doc)[1]
        return {"id": ref.id, **doc}
    except Exception as err:  # noqa: BLE001
        raise HTTPException(500, f"write failed: {err}") from err


@app.delete("/api/items/{item_id}", status_code=204)
def delete_item(item_id: str):
    client = db()
    if client is None:
        raise HTTPException(503, f"data store unavailable: {_db_error}")
    client.collection(COLLECTION).document(item_id).delete()
    # Returned explicitly rather than falling through on None. A 204 must not
    # carry a body, and leaving the framework to work that out from the
    # status code is a dependency on its version, not on anything stated here.
    return Response(status_code=204)


app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def root():
    return FileResponse("static/index.html")


if __name__ == "__main__":
    import uvicorn

    # Route uvicorn's own records through the JSON formatter instead of
    # disabling its logging entirely, which is what left this service silent.
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        lg = logging.getLogger(name)
        lg.handlers = [handler]
        lg.propagate = False
        lg.setLevel(logging.INFO)

    log("starting", port=PORT, collection=COLLECTION)

    # 0.0.0.0, not localhost - binding to loopback is the most common reason a
    # container that works locally never serves traffic on Cloud Run.
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_config=None)
