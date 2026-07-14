"""Dense embedding service adapters."""

from knowledgehub.embeddings.endpoint_pool import EndpointPool
from knowledgehub.embeddings.tei_client import TEIClient

__all__ = ["EndpointPool", "TEIClient"]
