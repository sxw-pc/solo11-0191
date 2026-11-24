"""Prepare offline emergency recall rounds.

The planner is deliberately pure and deterministic.  Every planned command has
a stable semantic content root and a round-specific idempotency token.  Receipt
evaluation can therefore distinguish a genuine current-round confirmation from
an old-round receipt that merely repeats the same ``incident_id``.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

DEFAULT_ACTIONS = {
    "driver": "evacuate",
    "operator": "evacuate",
    "machine_operator": "evacuate",
    "controller": "stop",
    "field_controller": "stop",
    "machine_controller": "stop",
    "site_lead": "confirm_stationary",
    "field_lead": "confirm_stationary",
    "field_manager": "confirm_stationary",
    "field_owner": "receive_completion",
}

DRIVER_ROLES = {"driver", "operator", "machine_operator"}
CONTROLLER_ROLES = {"controller", "field_controller", "machine_controller"}
SITE_LEAD_ROLES = {"site_lead", "field_lead", "field_manager", "site_manager"}

_STOP_STATES = {"stopped", "stationary", "safe_stop", "powered_off", "halted"}
_RUNNING_STATES = {"running", "moving", "resumed", "active", "started"}

# Transport/proof fields are not command semantics.  Tokens and roots are
# derived fields and must not feed back into the semantic root.
_NON_COMMAND_KEYS = {
    "signature",
    "sig",
    "algorithm",
    "token",
    "idempotency_key",
    "content_root",
    "command_root",
    "plan_root",
    "round",
    "round_no",
    "round_number",
    "round_id",
    "incident_id",
    "proof",
    "receipt_id",
}


class IncidentPlanError(ValueError):
    """Raised when an incident cannot be planned safely."""


def canonical_bytes(value: Any) -> bytes:
    """Return deterministic canonical JSON bytes."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def digest(value: Any) -> str:
    """SHA-256 hex digest of a value's canonical JSON representation."""
    if isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
    elif isinstance(value, str):
        raw = value.encode("utf-8")
    else:
        raw = canonical_bytes(value)
    return hashlib.sha256(raw).hexdigest()


def _pick(mapping: Any, *keys: str) -> Any:
    if not isinstance(mapping, dict):
        return None
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return value
    return None


def coerce_round(value: Any) -> Any:
    """Coerce common round labels (2, "2", "r2", "round-2") to integers."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if text.isdigit():
        return int(text)
    match = re.search(r"(\d+)$", text)
    if match:
        return int(match.group(1))
    return text


def incident_round(incident: dict[str, Any]) -> int:
    """Read the explicit round/revision fence.

    Missing rounds are treated as round one for compatibility with the seed
    fixture.  Round fencing is only meaningful when callers increment the
    explicit round after command changes.
    """
    value = _pick(incident, "round", "round_no", "round_number", "round_id")
    if value is None:
        return 1
    parsed = coerce_round(value)
    if isinstance(parsed, int):
        if parsed < 1:
            raise IncidentPlanError("round must be >= 1")
        return parsed
    raise IncidentPlanError(f"cannot parse numeric incident round: {value!r}")


def role_of(envelope: dict[str, Any] | None) -> str | None:
    return _pick(envelope, "party", "role", "actor", "actor_role", "source_role")


def receipt_round(envelope: dict[str, Any]) -> Any:
    return _pick(envelope, "round", "round_no", "round_number", "round_id")


def token_for(incident_id: str, round_no: int, command_root: str) -> str:
    """Derive the stable token for one command in one round."""
    return "idem-" + digest(
        {"incident_id": incident_id, "round": round_no, "command_root": command_root}
    )[:32]


def _semantic_command(raw: dict[str, Any]) -> dict[str, Any]:
    semantic = {
        key: value
        for key, value in raw.items()
        if key not in _NON_COMMAND_KEYS and not key.startswith("_")
    }
    return semantic


def normalize_command(
    command: Any, incident_id: str, round_no: int, index: int
) -> dict[str, Any]:
    """Normalize a string or mapping command and derive roots/tokens."""
    if isinstance(command, str):
        role = command.strip()
        if not role:
            raise IncidentPlanError("command role must not be empty")
        raw: dict[str, Any] = {"role": role}
    elif isinstance(command, dict):
        raw = dict(command)
        role = _pick(raw, "role", "party", "actor", "target_role")
        if not role:
            raise IncidentPlanError(f"command is missing role/party: {command!r}")
        role = str(role)
    else:
        raise IncidentPlanError(f"unsupported command type: {type(command).__name__}")

    action = _pick(raw, "action", "verb", "command", "type") or DEFAULT_ACTIONS.get(
        role, "acknowledge"
    )
    action = str(action)
    target = _pick(
        raw, "target", "target_id", "machine_id", "controller_id", "device_id"
    )
    command_id = str(_pick(raw, "command_id", "id") or f"{role}:{action}:{index + 1}")

    # The stable root deliberately excludes the round.  An unchanged evacuation
    # instruction therefore keeps the same command root across a plan expansion,
    # while its round-specific token still changes.
    root_input = {
        "incident_id": incident_id,
        "command_id": command_id,
        "role": role,
        "action": action,
        "target": target,
        "content": _semantic_command(raw),
    }
    command_root = digest(root_input)
    token = token_for(incident_id, round_no, command_root)

    is_controller_stop = role in CONTROLLER_ROLES and action in {
        "stop",
        "safe_stop",
        "halt",
        "power_off",
        "shutdown",
    }
    return {
        "index": index,
        "command_id": command_id,
        "role": role,
        "party": role,
        "action": action,
        "target": target,
        "command_root": command_root,
        "content_root": command_root,
        "token": token,
        "idempotency_key": token,
        "requires_stop_proof": is_controller_stop,
        "independent_evidence": role in CONTROLLER_ROLES,
        "raw": raw,
    }


def prepare(incident: dict[str, Any]) -> dict[str, Any]:
    """Prepare the current command round and its content/token fences.

    The original ``commands`` value is retained for compatibility.  Rich
    command objects are available as both ``planned_commands`` and
    ``command_objects``.
    """
    if not isinstance(incident, dict):
        raise IncidentPlanError("incident must be an object")
    incident_id = incident.get("incident_id") or incident.get("id")
    if not incident_id:
        raise IncidentPlanError("incident_id is required")
    if "commands" not in incident:
        raise IncidentPlanError("commands is required")
    raw_commands = incident["commands"]
    if not isinstance(raw_commands, (list, tuple)) or not raw_commands:
        raise IncidentPlanError("commands must be a nonempty list")

    round_ref = _pick(incident, "round", "round_no", "round_number", "round_id")
    round_no = incident_round(incident)
    planned = [
        normalize_command(command, str(incident_id), round_no, index)
        for index, command in enumerate(raw_commands)
    ]
    content_root = digest(
        {
            "incident_id": str(incident_id),
            "round": round_no,
            "commands": [item["command_root"] for item in planned],
        }
    )
    requirements = [
        {
            "role": item["role"],
            "party": item["party"],
            "command_id": item["command_id"],
            "action": item["action"],
            "token": item["token"],
            "command_root": item["command_root"],
            "requires_stop_proof": item["requires_stop_proof"],
        }
        for item in planned
    ]
    return {
        "incident_id": str(incident_id),
        "round": round_no,
        "round_no": round_no,
        "round_ref": round_ref if round_ref is not None else 1,
        "round_explicit": round_ref is not None,
        "content_root": content_root,
        "plan_root": content_root,
        # Preserve the seed API exactly: this is the caller-supplied value.
        "commands": raw_commands,
        "raw_commands": list(raw_commands),
        "planned_commands": planned,
        "command_objects": planned,
        "requirements": requirements,
        "required_roles": [item["role"] for item in planned],
    }


def is_stop_proven(receipt: dict[str, Any]) -> bool:
    """Return true only for an explicit, internally consistent stop state."""
    if not isinstance(receipt, dict):
        return False

    # Explicit negative signals win over loose convenience flags.  A envelope
    # that says both stop_proven=true and stopped=false is contradictory, not
    # proof.
    if receipt.get("stopped") is False or receipt.get("stop_confirmed") is False:
        return False
    state = str(
        _pick(receipt, "state", "controller_state", "status", "machine_state") or ""
    ).lower()
    if state in _RUNNING_STATES:
        return False

    if receipt.get("stop_proven") is True or receipt.get("controller_stopped") is True:
        return True
    if receipt.get("stopped") is True or receipt.get("stop_confirmed") is True:
        return True

    if state in _STOP_STATES:
        return True

    proof = receipt.get("proof")
    if isinstance(proof, dict):
        proof_state = str(
            _pick(proof, "state", "controller_state", "status") or ""
        ).lower()
        if proof.get("stopped") is False or proof_state in _RUNNING_STATES:
            return False
        if proof.get("stopped") is True or proof_state in _STOP_STATES:
            return True
    return False


def is_stationary_confirmation(confirmation: dict[str, Any]) -> bool:
    if not isinstance(confirmation, dict):
        return False
    if confirmation.get("machines_stationary") is True:
        return True
    if confirmation.get("stationary") is True:
        return True
    if confirmation.get("confirmed") is True and str(
        confirmation.get("fact", "")
    ) in {"stationary", "machines_stationary", "static"}:
        return True
    state = str(_pick(confirmation, "state", "machine_state") or "").lower()
    return state in {"stationary", "static", "immobile"}


def _root_matches(receipt: dict[str, Any], command: dict[str, Any], plan: dict[str, Any]) -> bool | None:
    """Return None when no root was supplied, otherwise whether it binds."""
    supplied = False
    for key in ("content_root", "plan_root"):
        value = receipt.get(key)
        if value is None:
            continue
        supplied = True
        if value == plan["content_root"] or value == command["command_root"]:
            return True
    command_root = receipt.get("command_root")
    if command_root is not None:
        supplied = True
        if command_root == command["command_root"]:
            return True
    return None if not supplied else False


def classify_receipt(
    receipt: dict[str, Any],
    command: dict[str, Any],
    plan: dict[str, Any],
    *,
    verifier=None,
    allow_carry_forward: bool = True,
    allow_unbound_legacy: bool = True,
) -> dict[str, Any]:
    """Classify one receipt against one planned command.

    The returned mapping always contains ``valid`` and a machine-readable
    ``disposition``.  A carried-forward receipt is valid evidence for an
    unchanged non-stop command, but is explicitly not a current-round fence
    crossing.
    """
    base = {
        "receipt_id": receipt.get("receipt_id") or receipt.get("id"),
        "role": role_of(receipt),
        "command_id": receipt.get("command_id"),
        "round": receipt_round(receipt),
        "receipt_index": None,
        "valid": False,
        "closes_requirement": False,
        "authoritative_state": False,
        "contradicts": False,
        "current_round": False,
        "carried_forward": False,
        "state_order": _envelope_order(receipt if isinstance(receipt, dict) else {}),
    }
    if not isinstance(receipt, dict):
        return {**base, "disposition": "not_an_object", "reason": "回执不是对象"}

    if str(receipt.get("incident_id")) != str(plan["incident_id"]):
        return {**base, "disposition": "wrong_incident", "reason": "incident_id 不匹配"}

    receipt_role = role_of(receipt)
    if receipt_role != command["role"]:
        return {**base, "disposition": "wrong_party", "reason": "回执角色与命令角色不匹配"}

    if base["command_id"] and base["command_id"] != command["command_id"]:
        return {**base, "disposition": "wrong_command", "reason": "command_id 不匹配"}

    if verifier is not None:
        try:
            signature_ok = verifier(receipt)
        except Exception as exc:  # verification failure is a rejection, not crash
            return {**base, "disposition": "bad_signature", "reason": str(exc)}
        if not signature_ok:
            return {**base, "disposition": "bad_signature", "reason": "签名校验失败"}

    root_match = _root_matches(receipt, command, plan)
    if root_match is False:
        return {**base, "disposition": "wrong_content_root", "reason": "内容根不匹配"}

    rr_raw = receipt_round(receipt)
    rr = coerce_round(rr_raw)
    if rr_raw is not None and not isinstance(rr, int):
        return {
            **base,
            "disposition": "malformed_round",
            "reason": "轮次标签无法解析为有序轮次，轮次栅栏拒绝该回执",
        }
    current_round = rr is not None and (
        rr == plan["round"] or rr == plan.get("round_ref")
    )
    if rr is not None and not current_round:
        # Verify an old token against the receipt's own round and stable root.
        receipt_token = receipt.get("token") or receipt.get("idempotency_key")
        if receipt_token:
            try:
                old_round = int(rr)
            except (TypeError, ValueError):
                old_round = None
            expected_old_token = (
                token_for(plan["incident_id"], old_round, command["command_root"])
                if old_round is not None
                else None
            )
            if expected_old_token and receipt_token != expected_old_token:
                return {**base, "disposition": "wrong_token", "reason": "旧轮次令牌不匹配"}

        can_carry = (
            allow_carry_forward
            and root_match is True
            and not command["requires_stop_proof"]
        )
        if can_carry:
            if command["requires_stop_proof"] and not is_stop_proven(receipt):
                return {**base, "disposition": "stop_not_proven", "reason": "缺少本轮独立停机证据"}
            return {
                **base,
                "valid": True,
                "closes_requirement": True,
                "carried_forward": True,
                "disposition": "carried_forward",
                "reason": "未变更指令的同内容根历史回执继续有效，但不能填补新增/变更轮次缺口",
            }
        return {
            **base,
            "disposition": "stale_round",
            "reason": "旧轮次回执被轮次栅栏拒绝，不能填补当前轮次缺口",
        }

    # A token, when present, must be this round's token.
    receipt_token = receipt.get("token") or receipt.get("idempotency_key")
    token_bound = receipt_token is not None
    if token_bound and receipt_token != command["token"]:
        return {**base, "disposition": "wrong_token", "reason": "幂等令牌/轮次不匹配"}

    if rr is None and not allow_unbound_legacy:
        return {**base, "disposition": "round_required", "reason": "回执缺少轮次栅栏"}
    if root_match is None and not allow_unbound_legacy:
        return {**base, "disposition": "content_root_required", "reason": "回执缺少内容根"}

    # Controller acknowledgement alone is not a stop state.  A current-round
    # running/revocation envelope is nevertheless valid authoritative state
    # evidence: it must be admitted so it can supersede an earlier stop proof
    # and force pending/correction rather than being silently discarded.
    closes_requirement = True
    authoritative_state = False
    contradicts = False
    if command["requires_stop_proof"]:
        if rr is None and root_match is not True and plan.get("round_explicit"):
            return {
                **base,
                "disposition": "round_required",
                "reason": "显式轮次的停机证明必须绑定轮次或当前内容根",
            }
        source_values = [
            _pick(receipt, "evidence_source", "source", "proof_source", "reported_by")
        ]
        proof = receipt.get("proof")
        if isinstance(proof, dict):
            source_values.append(
                _pick(proof, "evidence_source", "source", "proof_source", "reported_by")
            )
        source = str(next((value for value in source_values if value), "") or "").lower()
        if source in {"driver", "operator", "machine_operator"}:
            return {**base, "disposition": "not_independent", "reason": "驾驶员来源不能作为独立停机证据"}

        state = str(_pick(receipt, "state", "controller_state", "status") or "").lower()
        revocation = (
            receipt.get("stop_revoked") is True
            or receipt.get("stop_confirmed") is False
            or receipt.get("stopped") is False
            or state in _RUNNING_STATES
        )
        if is_stop_proven(receipt):
            authoritative_state = True
            disposition = "current_stop_proof"
            reason = "获得本轮次同内容/令牌的独立停机证据"
        elif revocation:
            authoritative_state = True
            contradicts = True
            closes_requirement = False
            disposition = "stop_revoked_or_running"
            reason = "控制器当前轮次证明未停机或撤销停机，原停机证据被后续状态覆盖"
        else:
            closes_requirement = False
            disposition = "stop_not_proven"
            reason = "控制器普通回执不等于已停机"

        return {
            **base,
            "valid": True,
            "closes_requirement": closes_requirement,
            "authoritative_state": authoritative_state,
            "contradicts": contradicts,
            "current_round": current_round,
            "token_bound": token_bound,
            "root_bound": root_match is True,
            "disposition": disposition,
            "reason": reason,
        }

    if receipt.get("received") is False or receipt.get("acknowledged") is False:
        return {
            **base,
            "valid": True,
            "closes_requirement": False,
            "authoritative_state": True,
            "contradicts": True,
            "current_round": current_round,
            "token_bound": token_bound,
            "root_bound": root_match is True,
            "disposition": "not_received",
            "reason": "回执明确表示未收到",
        }

    disposition = "current_match" if current_round else "legacy_unbound_match"
    return {
        **base,
        "valid": True,
        "closes_requirement": True,
        "current_round": current_round,
        "token_bound": token_bound,
        "root_bound": root_match is True,
        "disposition": disposition,
        "reason": "当前轮次回执有效" if current_round else "无轮次/内容根的兼容回执按角色匹配",
    }


def _envelope_order(envelope: dict[str, Any], receipt_index: int = 0) -> tuple[int, Any]:
    """Return a source-order key for controller state evidence."""
    sequence = _pick(
        envelope,
        "state_seq",
        "controller_seq",
        "status_seq",
        "sequence",
        "revision",
        "state_revision",
    )
    if sequence is not None:
        parsed = coerce_round(sequence)
        if isinstance(parsed, int):
            return (2, parsed)
        return (1, str(parsed))
    timestamp = _pick(envelope, "state_at", "observed_at", "issued_at", "timestamp")
    if timestamp is not None:
        return (0, str(timestamp))
    return (-1, receipt_index)


def _state_order(result: dict[str, Any]) -> tuple[int, Any]:
    return result.get("state_order") or (-1, result.get("receipt_index", 0))


def evaluate(
    incident: dict[str, Any],
    acknowledgements: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None,
    *,
    verifier=None,
    secrets: Any = None,
    strict: bool = False,
    allow_carry_forward: bool = True,
    allow_unbound_legacy: bool = True,
) -> dict[str, Any]:
    """Evaluate all role requirements for the current incident plan."""
    if strict:
        allow_carry_forward = False
        allow_unbound_legacy = False
    if verifier is None and secrets is not None:
        # Lazy import keeps incident.py independent from the recovery ledger.
        from .incident_capture import secret_for, verify_envelope

        def secrets_verifier(envelope: dict[str, Any]) -> bool:
            secret = secret_for(envelope, secrets)
            return bool(secret and verify_envelope(envelope, secret))

        verifier = secrets_verifier

    plan = incident if isinstance(incident, dict) and "planned_commands" in incident else prepare(incident)
    receipts = list(acknowledgements or [])
    requirement_results: list[dict[str, Any]] = []
    classifications: list[dict[str, Any]] = []

    for command in plan["planned_commands"]:
        command_results = []
        for receipt_index, receipt in enumerate(receipts):
            result = classify_receipt(
                receipt,
                command,
                plan,
                verifier=verifier,
                allow_carry_forward=allow_carry_forward,
                allow_unbound_legacy=allow_unbound_legacy,
            )
            result["command_id"] = command["command_id"]
            result["receipt_index"] = receipt_index
            if result.get("state_order", (-1,))[0] == -1:
                result["state_order"] = (-1, receipt_index)
            classifications.append(result)
            command_results.append(result)

        valid = [result for result in command_results if result["valid"]]
        closing = [result for result in valid if result.get("closes_requirement", True)]
        stateful = [result for result in valid if result.get("authoritative_state")]
        latest_state = max(stateful, key=_state_order) if stateful else None
        latest_closing = max(closing, key=_state_order) if closing else None
        explicit_state_order = bool(
            stateful and _state_order(latest_state)[0] >= 0
        )
        unordered_state_conflict = (
            bool(stateful)
            and any(result.get("contradicts") for result in stateful)
            and any(result.get("closes_requirement") for result in stateful)
            and not explicit_state_order
        )
        same_role_attempts = [
            result for result in command_results if result.get("role") == command["role"]
        ]
        last_attempt = same_role_attempts[-1] if same_role_attempts else None

        if unordered_state_conflict:
            # Fail closed: offline arrival order cannot tell whether an old stop
            # crossed a newer revocation when neither has a source order fence.
            satisfied = False
            best = latest_state
        elif latest_state is not None and latest_state.get("contradicts"):
            satisfied = False
            best = latest_state
        elif latest_closing is not None:
            satisfied = True
            best = latest_closing
        elif valid:
            satisfied = False
            best = valid[-1]
        else:
            satisfied = False
            best = None

        if unordered_state_conflict:
            disposition = "conflicting_state_evidence"
            reason = "同时存在停机与运行/撤销证据但缺少来源顺序栅栏，按未解除处理"
        else:
            disposition = best["disposition"] if best else (
                last_attempt["disposition"] if last_attempt else "missing"
            )
            reason = best["reason"] if best else (
                last_attempt["reason"] if last_attempt else "缺少本角色有效回执"
            )
        requirement_results.append(
            {
                "role": command["role"],
                "party": command["party"],
                "command_id": command["command_id"],
                "action": command["action"],
                "token": command["token"],
                "requires_stop_proof": command["requires_stop_proof"],
                "satisfied": satisfied,
                "current_round": bool(best and best.get("current_round")),
                "carried_forward": bool(best and best.get("carried_forward")),
                "matched_receipt_id": best.get("receipt_id") if best and satisfied else None,
                "authoritative_receipt_id": best.get("receipt_id") if best else None,
                "last_disposition": last_attempt["disposition"] if last_attempt else "missing",
                "disposition": disposition,
                "reason": reason,
            }
        )

    satisfied_all = all(item["satisfied"] for item in requirement_results)
    return {
        "status": "acknowledged" if satisfied_all else "pending",
        "incident_id": plan["incident_id"],
        "round": plan["round"],
        "content_root": plan["content_root"],
        "requirements": requirement_results,
        "classifications": classifications,
    }


def status(incident: dict[str, Any], acknowledgements=None, **options: Any) -> str:
    """Backwards-compatible status entry point returning only the status word."""
    return evaluate(incident, acknowledgements, **options)["status"]
