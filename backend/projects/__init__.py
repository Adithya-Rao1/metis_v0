"""Domain API router. `backend.main` mounts `router`; it includes every domain package's routes.

Routes needing a user depend on backend.accounts.deps.current_user (via backend.jobs.deps). Only the model
card routes (/models, /models/{name}) are public.
"""

from fastapi import APIRouter

from backend.compounds.routes import router as _compounds
from backend.exports.routes import router as _exports
from backend.predictions.routes import public_router as _models
from backend.predictions.routes import router as _predictions
from backend.projects.routes import router as _projects

router = APIRouter()
for _r in (_projects, _compounds, _predictions, _exports, _models):
    router.include_router(_r)

__all__ = ["router"]
