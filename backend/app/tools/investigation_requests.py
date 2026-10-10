"""Tool-specific public inputs and a stable legacy execution representation."""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator


TOOL_KINDS = {"resource_summary": "service", "registered_business": "service", "pod_logs": "pod",
    "pod_events": "pod", "endpoint_slice": "endpoint_slice", "deployment": "deployment", "replica_set": "replica_set"}


class ToolBoundaryError(ValueError):
    """An unavailable tool/resource is not eligible for parameter correction."""


class RequestBase(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    resource_ref: str = Field(pattern=r"^ref-[a-f0-9]{24}$")


class LogRequest(RequestBase):
    tool: Literal["pod_logs"]
    previous: bool = False
    tail_lines: int = Field(default=100, ge=1, le=1000)


class ResourceRequest(RequestBase):
    tool: Literal["resource_summary", "registered_business", "pod_events", "endpoint_slice", "deployment", "replica_set"]

    @model_validator(mode="before")
    @classmethod
    def legacy_defaults(cls, value):
        # Only inert defaults from old persisted requests are compatible. Never
        # silently discard meaningful values, arbitrary fields, or coerced types.
        if isinstance(value, dict):
            value = dict(value)
            if value.get("previous") is False:
                value.pop("previous")
            if type(value.get("tail_lines")) is int and value["tail_lines"] == 100:
                value.pop("tail_lines")
        return value


AgentToolRequest = Annotated[LogRequest | ResourceRequest, Field(discriminator="tool")]
REQUEST_ADAPTER = TypeAdapter(AgentToolRequest)


class ToolRequest(BaseModel):
    """Keep field order/defaults identical for saved tool fingerprints and replay."""
    model_config = ConfigDict(extra="forbid", strict=True)
    tool: Literal["resource_summary", "registered_business", "pod_logs", "pod_events", "endpoint_slice", "deployment", "replica_set"]
    resource_ref: str = Field(pattern=r"^ref-[a-f0-9]{24}$")
    previous: bool = False
    tail_lines: int = Field(default=100, ge=1, le=1000)

    @model_validator(mode="before")
    @classmethod
    def validate_public_contract(cls, value):
        if isinstance(value, cls):
            return value
        if isinstance(value, BaseModel):
            value = value.model_dump()
        return REQUEST_ADAPTER.validate_python(value).model_dump()
