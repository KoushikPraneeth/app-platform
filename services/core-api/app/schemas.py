from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel, ConfigDict, Field


class ItemBase(BaseModel):
    username: str = Field(
        ...,
        min_length=1,
        max_length=100,
        description="User handle, e.g. @alex_devops",
        examples=["@alex_devops"],
    )
    community: str = Field(
        default="r/general",
        min_length=1,
        max_length=100,
        description="Target community board, e.g. r/kubernetes, r/azure",
        examples=["r/kubernetes"],
    )
    content: str = Field(
        ...,
        min_length=1,
        description="Post message or cluster operational update",
        examples=["Just rolled out ArgoCD Umbrella Helm chart v1.4 to production AKS!"],
    )


class ItemCreate(ItemBase):
    pass


class ItemResponse(ItemBase):
    id: int
    upvotes: int = 0
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ItemListResponse(BaseModel):
    source: str = Field(..., description="Data source: 'cache' or 'database'")
    items: List[ItemResponse]


class UpvoteResponse(BaseModel):
    id: int
    upvotes: int
    status: str = "success"


class HealthResponse(BaseModel):
    status: str


class ReadinessResponse(BaseModel):
    status: str
    postgres: str
    redis: str
    details: Optional[str] = None
