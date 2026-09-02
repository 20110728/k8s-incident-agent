from backend.app.diagnosis.rules import (
    RULES,
    unknown_rule,
)
from backend.app.schemas.diagnosis import DiagnosisResult
from backend.app.schemas.kubernetes import (
    ServiceEvidenceBundle,
)
from backend.app.tools.client import KubernetesClients
from backend.app.tools.evidence_collector import (
    collect_service_evidence,
)

ALLOWED_NAMESPACES = {"agent-demo"}


def diagnose_evidence(
    bundle: ServiceEvidenceBundle,
) -> DiagnosisResult:
    for rule in RULES:
        result = rule(bundle)

        if result is not None:
            return result

    return unknown_rule(bundle)


def diagnose_service(
    clients: KubernetesClients,
    namespace: str,
    service_name: str,
) -> DiagnosisResult:
    if namespace not in ALLOWED_NAMESPACES:
        raise ValueError(
            f"Namespace {namespace!r} is not allowed. "
            f"Allowed namespaces: "
            f"{sorted(ALLOWED_NAMESPACES)}"
        )

    evidence = collect_service_evidence(
        clients=clients,
        namespace=namespace,
        service_name=service_name,
    )

    return diagnose_evidence(evidence)
