# 版本化服务配置的数据契约：登记资源关联、负责人、镜像版本、探针与业务断言。
# 严格拒绝未知字段；业务检查只允许预登记的只读 GET 约定。

from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Name = Annotated[str, Field(min_length=1, max_length=253, pattern=r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$")]
Text = Annotated[str, Field(min_length=1, max_length=300)]
Port = Annotated[int, Field(strict=True, ge=1, le=65535)] | Name


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class HttpProbeContract(ContractModel):
    path: Text
    port: Port
    scheme: Literal["HTTP", "HTTPS"] = "HTTP"

    @field_validator("path")
    @classmethod
    def local_path(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (not value.startswith("/") or value.startswith("//")
                or parsed.netloc or parsed.scheme or parsed.query or parsed.fragment
                or "\\" in value or any(c.isspace() or ord(c) < 32 for c in value)):
            raise ValueError("only a local absolute path without query/fragment is allowed")
        return value


class BusinessCheckContract(HttpProbeContract):
    check_id: Name
    method: Literal["GET"] = "GET"
    expected_status: Annotated[int, Field(ge=200, le=299)] = 200
    expected_json_subset: dict[str, str | int | bool] = Field(default_factory=dict)
    timeout_seconds: Annotated[int, Field(ge=1, le=10)] = 3
    max_response_bytes: Annotated[int, Field(ge=1, le=65536)] = 16384
    follow_redirects: Literal[False] = False
    read_only: Literal[True] = True


class Owner(ContractModel):
    team: Text
    contact: Text


class ApplicationContract(ContractModel):
    version: Text
    version_label: Literal["app.kubernetes.io/version"] = "app.kubernetes.io/version"
    images: dict[Name, Text] = Field(min_length=1)


class ServiceProfile(ContractModel):
    schema_version: Literal["v1"]
    profile_id: Name
    revision: Text
    namespace: Name
    service_name: Name
    deployment_name: Name
    container_name: Name
    owner: Owner
    config_source: Text
    application: ApplicationContract
    expected_selector: dict[Text, Text] = Field(min_length=1)
    # Optional for legacy profiles; Demo explicitly registers its replica contract.
    expected_replicas: Annotated[int, Field(strict=True, ge=1)] | None = None
    readiness_probe: HttpProbeContract
    liveness_probe: HttpProbeContract
    business_checks: list[BusinessCheckContract] = Field(max_length=10)

    @model_validator(mode="after")
    def check_references(self):
        if self.container_name not in self.application.images:
            raise ValueError("container_name must exist in application.images")
        ids = [check.check_id for check in self.business_checks]
        if len(ids) != len(set(ids)):
            raise ValueError("business check IDs must be unique")
        return self
