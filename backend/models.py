from pydantic import BaseModel, Field
from typing import Any, Dict, List, Optional
import uuid
import time


class SessionState(BaseModel):
    session_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    stage: str = "full_name"
    slots: Dict[str, Any] = Field(default_factory=dict)
    available_plans: List[Dict[str, Any]] = Field(default_factory=list)
    partial_inputs: Dict[str, str] = Field(default_factory=dict)
    voice_fail_count: int = 0
    resume_stage: Optional[str] = None
    created_at: float = Field(default_factory=time.time)


class ManualInput(BaseModel):
    field: str
    value: str
