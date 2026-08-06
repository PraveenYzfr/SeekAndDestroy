from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class RetrievalDocument(BaseModel):
    id: str
    text: str
    entity_type: str  # app.models.enums.EntityKind value
    entity_id: int
    environment: Optional[str] = None
    platform: Optional[str] = None
    location: Optional[str] = None
    lifecycle_status: Optional[str] = None
    availability_tier: Optional[str] = None
    compliance_classification: Optional[str] = None
    source_timestamp: Optional[datetime] = None

    def metadata(self) -> dict:
        return {
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "environment": self.environment,
            "platform": self.platform,
            "location": self.location,
            "lifecycle_status": self.lifecycle_status,
            "availability_tier": self.availability_tier,
            "compliance_classification": self.compliance_classification,
            "source_timestamp": self.source_timestamp.isoformat() if self.source_timestamp else None,
        }


class SearchResult(BaseModel):
    document: RetrievalDocument
    score: float
