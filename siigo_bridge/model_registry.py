"""Public identity and manufacturer budgets; gateway capacity remains unverified."""
from importlib.resources import files
import json

from .auth import BridgeError

MODEL_REGISTRY = json.loads(files("siigo_bridge").joinpath("model_registry.json").read_text(encoding="utf-8"))
MODELS_BY_ID = {model["public_id"]: model for model in MODEL_REGISTRY}


def resolve_model(public_id: str, api: str) -> str:
    """Resolve only advertised IDs; do not silently substitute models or protocols."""
    model = MODELS_BY_ID.get(public_id)
    if model is None:
        raise BridgeError("Unknown model; use GET /v1/models", "model_not_found", 404)
    if model["api"] != api:
        raise BridgeError("Model requires its advertised API protocol", "model_api_mismatch", 400)
    return model["upstream_id"]
