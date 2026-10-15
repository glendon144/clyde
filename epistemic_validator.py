"""Local, heuristic output assurance for AI Broker.

This module is deliberately not a truth oracle.  It looks for results that are
structurally incomplete, claim consequential actions without execution
evidence, violate caller-declared expectations, or lack the identifiers that a
successful write normally returns.  Suspicious and indeterminate results are
preserved for review; the caller decides whether to accept them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

ASSURANCE_VERSION = "0.1"
STATUS_CLEAR = "clear"
STATUS_SUSPICIOUS = "suspicious"
STATUS_INDETERMINATE = "indeterminate"

_ACTION_CLAIM = re.compile(
    r"\b(?:i|we)\s+(?:have\s+)?(?:successfully\s+)?"
    r"(?:attached|sent|emailed|created|deleted|uploaded|downloaded|saved|"
    r"deployed|installed|updated|modified|moved|copied|scheduled)\b",
    re.IGNORECASE,
)
_WRITE_INTENT = re.compile(
    r"(?:attach|capture|create|delete|deploy|install|move|send|store|update|upload|write)",
    re.IGNORECASE,
)
_EVIDENCE_KEYS = {
    "artifact_id",
    "artifacts",
    "capsule_id",
    "capsule_uuid",
    "evidence",
    "execution_record",
    "file_id",
    "message_id",
    "object_id",
    "provenance",
    "receipt",
    "tool_calls",
    "version_id",
}


@dataclass(frozen=True)
class Signal:
    code: str
    severity: str
    score: float
    message: str

    def as_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "score": self.score,
            "message": self.message,
        }


def _lookup(mapping: Any, dotted_path: str) -> Any:
    current = mapping
    for part in str(dotted_path).split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return True


def _has_evidence(result: Any, metadata: Any) -> bool:
    for container in (result, metadata):
        if not isinstance(container, dict):
            continue
        for key in _EVIDENCE_KEYS:
            if _present(container.get(key)):
                return True
    return False


def _result_text(result: Any) -> str:
    if isinstance(result, str):
        return result.strip()
    if isinstance(result, dict):
        return str(result.get("text") or result.get("summary") or "").strip()
    return ""


def _required_fields(
    container: Any,
    fields: Iterable[Any],
    namespace: str,
) -> List[Signal]:
    signals: List[Signal] = []
    for field in fields:
        path = str(field).strip()
        if path and not _present(_lookup(container, path)):
            signals.append(
                Signal(
                    code=f"missing_required_{namespace}_field",
                    severity="high",
                    score=0.9,
                    message=f"Required {namespace} field '{path}' is missing or empty.",
                )
            )
    return signals


def assess_output(
    task: Dict[str, Any],
    response: Dict[str, Any],
    provider: str,
) -> Dict[str, Any]:
    """Return a machine- and human-readable assurance assessment.

    ``response`` is the raw successful provider-adapter response.  Provider
    transport failures should remain ordinary execution failures and should not
    be passed here.
    """
    result = response.get("result")
    metadata = response.get("metadata") or {}
    task_type = str(task.get("type") or "")
    intent = str(task.get("intent") or "")
    expectations = task.get("expect") or {}
    if not isinstance(expectations, dict):
        expectations = {}

    signals: List[Signal] = []

    if result is None:
        signals.append(
            Signal(
                code="missing_result",
                severity="high",
                score=0.95,
                message="The provider reported success without returning a result.",
            )
        )

    signals.extend(
        _required_fields(
            result,
            expectations.get("required_result_fields", []) or [],
            "result",
        )
    )
    signals.extend(
        _required_fields(
            metadata,
            expectations.get("required_metadata_fields", []) or [],
            "metadata",
        )
    )

    if expectations.get("evidence_required") and not _has_evidence(result, metadata):
        signals.append(
            Signal(
                code="required_evidence_missing",
                severity="high",
                score=0.9,
                message="The task required execution evidence, but the result supplied none.",
            )
        )

    if isinstance(metadata, dict) and metadata.get("mediated") is False:
        signals.append(
            Signal(
                code="unmediated_execution_path",
                severity="high",
                score=0.9,
                message=(
                    "The result came from an execution path that did not pass through "
                    "normal broker mediation."
                ),
            )
        )

    if (
        task_type in {"llm", "ocr", "mcp"}
        and result is not None
        and not _result_text(result)
    ):
        signals.append(
            Signal(
                code="empty_text_result",
                severity="high",
                score=0.9,
                message="The provider reported success but returned no usable text.",
            )
        )

    text = _result_text(result)
    if _ACTION_CLAIM.search(text) and not _has_evidence(result, metadata):
        signals.append(
            Signal(
                code="unsupported_action_claim",
                severity="high",
                score=0.85,
                message=(
                    "The output claims that an external action was completed, but the "
                    "provider returned no execution evidence or artifact identifier."
                ),
            )
        )

    if task_type == "shell" and isinstance(result, dict):
        returncode = result.get("returncode")
        if returncode is None:
            signals.append(
                Signal(
                    code="shell_returncode_missing",
                    severity="high",
                    score=0.95,
                    message="Shell execution returned no process return code.",
                )
            )
        elif returncode != 0:
            signals.append(
                Signal(
                    code="shell_command_failed",
                    severity="high",
                    score=0.98,
                    message=f"The shell process exited with non-zero status {returncode}.",
                )
            )

    if (
        task_type == "dream"
        and intent == "store_milestone"
        and isinstance(result, dict)
    ):
        if not (
            _present(result.get("capsule_id")) or _present(result.get("capsule_uuid"))
        ):
            signals.append(
                Signal(
                    code="write_receipt_missing",
                    severity="high",
                    score=0.9,
                    message="Dream reported a successful write without a capsule identifier.",
                )
            )

    if (
        task_type == "mcp"
        and _WRITE_INTENT.search(intent)
        and not _has_evidence(result, metadata)
    ):
        signals.append(
            Signal(
                code="mcp_write_receipt_missing",
                severity="medium",
                score=0.7,
                message=(
                    "A write-like MCP operation returned no recognizable receipt or "
                    "object identifier."
                ),
            )
        )

    if isinstance(result, dict) and _present(result.get("substitution")):
        substitution_policy = str(expectations.get("substitution_policy", "review"))
        if substitution_policy != "allow":
            signals.append(
                Signal(
                    code="substitution_requires_review",
                    severity="high",
                    score=0.9,
                    message="The result reports a substitution that was not pre-authorized.",
                )
            )

    # A missing result is not enough evidence to call the output suspicious in
    # a substantive sense; it is indeterminate.  Other signals identify a
    # concrete mismatch and are classified as suspicious.
    if signals and all(signal.code == "missing_result" for signal in signals):
        status = STATUS_INDETERMINATE
    elif signals:
        status = STATUS_SUSPICIOUS
    else:
        status = STATUS_CLEAR

    requires_review = status != STATUS_CLEAR
    risk_score = max((signal.score for signal in signals), default=0.0)
    if requires_review:
        human_summary = (
            f"Human approval recommended: {len(signals)} assurance signal(s) "
            "were raised. The output is preserved but is not verified complete."
        )
    else:
        human_summary = (
            "No obvious assurance problems were detected by the local checks. "
            "This is not a guarantee that the output is factually correct."
        )

    return {
        "version": ASSURANCE_VERSION,
        "method": "local_heuristics",
        "status": status,
        "completion_disposition": (
            "pending_human_approval" if requires_review else "provisionally_clear"
        ),
        "requires_human_approval": requires_review,
        "hallucination_risk_score": round(risk_score, 2),
        "human_summary": human_summary,
        "signals": [signal.as_dict() for signal in signals],
        "limitations": [
            "Heuristics detect warning signs, not truth or deception.",
            "A clear result may still contain factual or semantic errors.",
        ],
        "provider": provider,
    }
