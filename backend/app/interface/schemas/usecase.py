from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel


class UseCaseResponse(BaseModel):
    name: str
    version: str
    description: str = ""
    supported_body_parts: list[str] = []
    required_sequences: list[str] = []
    model_type: str
    enabled: bool = True
    module_path: str = ""
    registered_at: datetime | None = None


class UseCaseListResponse(BaseModel):
    usecases: list[UseCaseResponse]


class RoutingRuleResponse(BaseModel):
    usecase_name: str
    rules: list[dict[str, Any]]


class RoutingRulesResponse(BaseModel):
    routing_rules: dict[str, list[dict[str, Any]]]


class UpdateRoutingRulesRequest(BaseModel):
    rules: list[dict[str, Any]]
