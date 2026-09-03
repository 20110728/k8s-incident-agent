from __future__ import annotations

import argparse
from pprint import pprint
from typing import Any
from uuid import uuid4

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from backend.app.agent.dependencies import (
    build_diagnosis_service,
    build_kubernetes_collector,
    build_remediation_planner,
    build_runbook_retriever,
)
from backend.app.agent.graph import (
    build_incident_graph,
)


def model_to_dict(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")

    return value


def read_approval_choice(
    configured_choice: str | None,
) -> str:
    if configured_choice is not None:
        return configured_choice

    while True:
        value = input(
            "请输入审批决定 [approve/reject]: "
        ).strip().lower()

        if value in {"approve", "reject"}:
            return value

        print(
            "无效输入，只能输入 approve 或 reject。"
        )


def read_approver(
    configured_approver: str | None,
) -> str:
    if configured_approver:
        normalized = configured_approver.strip()
        if normalized:
            return normalized

    while True:
        value = input(
            "请输入审批人标识: "
        ).strip()

        if value:
            return value

        print("审批人标识不能为空。")


def print_result(
    result: dict[str, Any],
    thread_id: str,
) -> None:
    pprint(
        {
            "thread_id": thread_id,
            "incident_id": result.get(
                "incident_id"
            ),
            "phase": result.get("phase"),
            "diagnosis": model_to_dict(
                result.get("diagnosis")
            ),
            "remediation_plan": model_to_dict(
                result.get("remediation_plan")
            ),
            "risk_level": result.get(
                "risk_level"
            ),
            "requires_approval": result.get(
                "requires_approval"
            ),
            "approval_status": result.get(
                "approval_status"
            ),
            "approval_request": model_to_dict(
                result.get("approval_request")
            ),
            "approval_record": model_to_dict(
                result.get("approval_record")
            ),
            "approved": result.get(
                "approved"
            ),
            "diagnosis_llm_model": result.get(
                "diagnosis_llm_model"
            ),
            "diagnosis_llm_usage": result.get(
                "diagnosis_llm_usage"
            ),
            "remediation_llm_model": result.get(
                "remediation_llm_model"
            ),
            "remediation_llm_usage": result.get(
                "remediation_llm_usage"
            ),
            "errors": result.get(
                "errors",
                [],
            ),
            "trace": result.get(
                "trace",
                [],
            ),
        }
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Run Kubernetes incident diagnosis "
            "and human approval workflow."
        )
    )
    parser.add_argument(
        "--namespace",
        default="agent-demo",
    )
    parser.add_argument(
        "--service",
        default="order-service",
    )
    parser.add_argument(
        "--description",
        required=True,
    )
    parser.add_argument(
        "--thread-id",
        default=None,
        help=(
            "LangGraph thread ID. A random UUID is "
            "generated when omitted."
        ),
    )
    parser.add_argument(
        "--approval",
        choices=("approve", "reject"),
        default=None,
        help=(
            "Explicit approval decision. When omitted, "
            "the program prompts interactively."
        ),
    )
    parser.add_argument(
        "--approver",
        default=None,
        help=(
            "Human approver identifier. Required only "
            "when an approval interrupt occurs."
        ),
    )
    parser.add_argument(
        "--comment",
        default="",
        help="Optional approval comment.",
    )
    args = parser.parse_args()

    thread_id = (
        args.thread_id.strip()
        if args.thread_id
        else str(uuid4())
    )

    graph = build_incident_graph(
        collector=build_kubernetes_collector(),
        retriever=build_runbook_retriever(),
        diagnoser=build_diagnosis_service(),
        planner=build_remediation_planner(),
        checkpointer=InMemorySaver(),
    )

    config = {
        "configurable": {
            "thread_id": thread_id,
        }
    }

    result = graph.invoke(
        {
            "request": {
                "namespace": args.namespace,
                "service_name": args.service,
                "description": args.description,
            }
        },
        config=config,
    )

    interrupts = result.get(
        "__interrupt__",
        (),
    )

    if interrupts:
        interrupt_value = interrupts[0].value
        approval_request = interrupt_value[
            "approval_request"
        ]

        print("\n工作流已暂停，等待人工审批：")
        pprint(interrupt_value)

        choice = read_approval_choice(
            args.approval
        )
        approver = read_approver(
            args.approver
        )

        result = graph.invoke(
            Command(
                resume={
                    "approval_id": approval_request[
                        "approval_id"
                    ],
                    "approved": (
                        choice == "approve"
                    ),
                    "approver": approver,
                    "comment": args.comment,
                }
            ),
            config=config,
        )

    print_result(
        result=result,
        thread_id=thread_id,
    )


if __name__ == "__main__":
    main()