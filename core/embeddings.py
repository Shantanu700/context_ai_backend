"""Local sentence-transformers embeddings. Loaded lazily — only workers should pay for it."""

import threading

from django.conf import settings

_model = None
_lock = threading.Lock()  # worker uses a thread pool; load the model exactly once


def _get_model():
    global _model
    if _model is not None:
        return _model
    with _lock:
        if _model is not None:
            return _model
        from sentence_transformers import SentenceTransformer

        # CPU on purpose: sentence-transformers otherwise picks MPS on Apple Silicon, and
        # torch's Metal kernels segfault when driven from several worker threads at once
        # (EXC_BAD_ACCESS in MetalShaderLibrary::exec_unary_kernel). A 22M-param model is
        # milliseconds on CPU anyway. Override with EMBEDDING_DEVICE if your GPU behaves.
        model = SentenceTransformer(settings.EMBEDDING_MODEL, device=settings.EMBEDDING_DEVICE)
        # renamed in sentence-transformers 6; keep working on either
        get_dim = getattr(model, "get_embedding_dimension", None) or model.get_sentence_embedding_dimension
        dim = get_dim()
        if dim != settings.EMBEDDING_DIM:
            raise RuntimeError(
                f"{settings.EMBEDDING_MODEL} emits {dim}-d vectors but EMBEDDING_DIM is "
                f"{settings.EMBEDDING_DIM} — the VectorField columns would reject them"
            )
        _model = model
        return _model


def embed(texts: list[str]) -> list[list[float]]:
    """Normalized, so cosine distance is a straight 1 - similarity."""
    return _get_model().encode(texts, normalize_embeddings=True).tolist()
