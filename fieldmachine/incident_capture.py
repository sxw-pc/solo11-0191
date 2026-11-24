"""Transactional offline capture for emergency command recovery.

This module is the recovery boundary between four things that must not be
conflated:

1. a durable outbound *intent* and its idempotency token;
2. an external controller's idempotent handling of that token;
3. content/round-fenced receipts and independent stop evidence;
4. append-only local completion/correction notifications.

The store is an in-memory, JSON-serializable transactional state.  Tests can
:meth:`IncidentCapture.snapshot` committed state at a power-loss point and
:meth:`IncidentCapture.restore` it into a new process/local input.
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
from contextlib import contextmanager
from typing import Any, Callable

from .incident import (
    CONTROLLER_ROLES,
    SITE_LEAD_ROLES,
    canonical_bytes,
    classify_receipt,
    coerce_round,
    digest,
    evaluate,
    is_stationary_confirmation,
    is_stop_proven,
    prepare,
    role_of,
)

COMPLETION_TYPE = "incident_resolution_completed"
CORRECTION_TYPE = "incident_completion_correction"


class CaptureError(ValueError, RuntimeError):
    """Base class for capture invariant failures."""


class IdempotencyConflict(CaptureError):
    """A reused idempotency key carries a different action/body."""


class IncidentNotResolved(CaptureError):
    """A completion notification was attempted before closure evidence."""


class RoundConflict(CaptureError):
    """A round fence was violated, e.g. same round with changed content."""


class PowerLoss(RuntimeError):
    """Optional explicit marker for a simulated crash before a watermark."""


def signing_payload(envelope: dict[str, Any]) -> bytes:
    body = {key: value for key, value in envelope.items() if key != "signature"}
    return canonical_bytes(body)


def sign_envelope(envelope: dict[str, Any], secret: str) -> dict[str, Any]:
    """Return an HMAC-SHA256 signed copy of an envelope."""
    if not secret:
        raise CaptureError("signing secret is required")
    signed = dict(envelope)
    signed["algorithm"] = "HS256"
    signature = hmac.new(
        secret.encode("utf-8"), signing_payload(signed), hashlib.sha256
    ).hexdigest()
    signed["signature"] = signature
    return signed


def verify_envelope(envelope: dict[str, Any], secret: str) -> bool:
    if not isinstance(envelope, dict) or not envelope.get("signature") or not secret:
        return False
    expected = hmac.new(
        secret.encode("utf-8"), signing_payload(envelope), hashlib.sha256
    ).hexdigest()
    try:
        return hmac.compare_digest(str(envelope["signature"]), expected)
    except TypeError:
        return False


def secret_for(envelope: dict[str, Any], secrets: Any) -> str | None:
    if secrets is None:
        return None
    if isinstance(secrets, str):
        return secrets
    if isinstance(secrets, dict):
        role = role_of(envelope) or envelope.get("signer") or envelope.get("key_id")
        for key in (role, f"{role}_secret" if role else None, envelope.get("key_id")):
            if key and key in secrets:
                return secrets[key]
    return None


def receipt_identity(envelope: dict[str, Any]) -> tuple[str, str]:
    """Return ``(deduplication_key, body_hash)`` for an inbound envelope."""
    receipt_id = envelope.get("receipt_id") or envelope.get("id") or envelope.get("delivery_id")
    body_hash = digest({key: value for key, value in envelope.items() if key != "signature"})
    if receipt_id:
        return f"id:{receipt_id}", body_hash
    return f"hash:{digest(envelope)}", body_hash


def _new_state() -> dict[str, Any]:
    return {
        "version": 1,
        "seq": 0,
        "incident": None,
        "plan": None,
        "plans": [],
        "receipts_by_key": {},
        "receipt_order": [],
        "intents": {},
        "watermarks": {},
        "confirmations": {},
        "confirmation_order": [],
        "notifications": [],
        "controller_queries": [],
        "recovery_actions": [],
        "audit": [],
    }


class ControllerGateway:
    """A small external controller simulator with token idempotency."""

    def __init__(self, secret: str | None = None):
        self.secret = secret
        self.records: dict[str, dict[str, Any]] = {}
        self.action_log: list[dict[str, Any]] = []
        self.send_calls: list[dict[str, Any]] = []

    def _maybe_sign(self, envelope: dict[str, Any]) -> dict[str, Any]:
        return sign_envelope(envelope, self.secret) if self.secret else dict(envelope)

    def send(self, intent: dict[str, Any]) -> dict[str, Any]:
        token = intent["token"]
        self.send_calls.append({"token": token, "action": intent.get("action")})
        existing = self.records.get(token)
        if existing is not None:
            if existing.get("action") != intent.get("action"):
                raise IdempotencyConflict(
                    f"token {token} cannot switch action "
                    f"{existing.get('action')!r} -> {intent.get('action')!r}"
                )
            if existing.get("command_root") != intent.get("command_root"):
                raise IdempotencyConflict(
                    f"token {token} cannot bind two command contents"
                )
            existing["delivery_count"] = int(existing.get("delivery_count", 1)) + 1
            result = copy.deepcopy(existing)
            result["duplicate"] = True
            return result

        record = {
            "incident_id": intent["incident_id"],
            "round": intent["round"],
            "party": intent.get("role") or intent.get("party") or "controller",
            "role": intent.get("role") or "controller",
            "token": token,
            "content_root": intent.get("content_root"),
            "command_root": intent.get("command_root"),
            "action": intent.get("action", "stop"),
            "state": "accepted",
            "landed": True,
            "delivery_count": 1,
        }
        self.records[token] = record
        self.action_log.append(
            {"token": token, "action": record["action"], "round": record["round"]}
        )
        result = copy.deepcopy(record)
        result["duplicate"] = False
        return result

    def seed(self, token: str, **changes: Any) -> dict[str, Any]:
        record = self.records.setdefault(
            token,
            {
                "token": token,
                "party": "controller",
                "role": "controller",
                "action": "stop",
                "delivery_count": 1,
            },
        )
        record.update(changes)
        return copy.deepcopy(record)

    def seed_stopped(self, token: str, **changes: Any) -> dict[str, Any]:
        return self.seed(
            token,
            landed=True,
            state="stopped",
            controller_state="stopped",
            stopped=True,
            stop_proven=True,
            **changes,
        )

    def query(self, query: dict[str, Any] | str) -> dict[str, Any]:
        token = query if isinstance(query, str) else query["token"]
        record = self.records.get(token)
        if record is None:
            result = {
                "incident_id": None if isinstance(query, str) else query.get("incident_id"),
                "round": None if isinstance(query, str) else query.get("round"),
                "party": "controller",
                "role": "controller",
                "token": token,
                "landed": False,
                "state": "not_landed",
            }
        else:
            result = copy.deepcopy(record)
            result["queried"] = True
        return self._maybe_sign(result)


class IncidentCapture:
    """Transactional capture ledger and recovery coordinator."""

    def __init__(
        self,
        incident: dict[str, Any] | None = None,
        *,
        acknowledgements: list[dict[str, Any]] | None = None,
        secrets: Any = None,
        state: dict[str, Any] | None = None,
        gateway: ControllerGateway | None = None,
    ):
        self.secrets = secrets
        self.gateway = gateway
        self.state = copy.deepcopy(state) if state is not None else _new_state()
        if incident is not None:
            self.prepare_round(incident)
        for ack in acknowledgements or []:
            self.receive_receipt(ack)

    # ---------- persistence / transactions ----------

    @contextmanager
    def _transaction(self):
        committed = copy.deepcopy(self.state)
        try:
            yield
        except Exception:
            self.state = committed
            raise

    def _next_seq(self) -> int:
        self.state["seq"] += 1
        return self.state["seq"]

    def _audit(self, event: str, **fields: Any) -> dict[str, Any]:
        entry = {"seq": self._next_seq(), "event": event, **fields}
        self.state["audit"].append(entry)
        return entry

    def snapshot(self) -> dict[str, Any]:
        """Return a durable committed-state checkpoint."""
        return copy.deepcopy(self.state)

    checkpoint = snapshot

    def to_json(self) -> str:
        return json.dumps(self.state, ensure_ascii=False, sort_keys=True)

    @classmethod
    def restore(
        cls, snapshot: dict[str, Any] | str, **kwargs: Any
    ) -> "IncidentCapture":
        if isinstance(snapshot, str):
            snapshot = json.loads(snapshot)
        return cls(state=copy.deepcopy(snapshot), **kwargs)

    # ---------- planning ----------

    @property
    def plan(self) -> dict[str, Any]:
        plan = self.state.get("plan")
        if not plan:
            raise CaptureError("no incident round has been prepared")
        return plan

    def prepare_round(self, incident: dict[str, Any]) -> dict[str, Any]:
        plan = prepare(incident)
        with self._transaction():
            old = self.state.get("plan")
            if old and old["incident_id"] != plan["incident_id"]:
                raise RoundConflict("capture ledger is bound to another incident_id")
            if old:
                old_round = int(old["round"])
                new_round = int(plan["round"])
                if new_round < old_round:
                    raise RoundConflict(
                        f"round fence refuses older round {new_round} after {old_round}"
                    )
                if new_round == old_round and old["content_root"] != plan["content_root"]:
                    raise RoundConflict(
                        "same round cannot change command content; open a new round"
                    )
            self.state["incident"] = copy.deepcopy(incident)
            self.state["plan"] = copy.deepcopy(plan)
            if not any(p["content_root"] == plan["content_root"] for p in self.state["plans"]):
                self.state["plans"].append(
                    {
                        "incident_id": plan["incident_id"],
                        "round": plan["round"],
                        "content_root": plan["content_root"],
                        "required_roles": list(plan["required_roles"]),
                        "seq": self._next_seq(),
                    }
                )
            self._audit(
                "round_prepared",
                incident_id=plan["incident_id"],
                round=plan["round"],
                content_root=plan["content_root"],
                roles=plan["required_roles"],
            )
        return copy.deepcopy(plan)

    def _verifier(self) -> Callable[[dict[str, Any]], bool] | None:
        if self.secrets is None:
            return None

        def verify(envelope: dict[str, Any]) -> bool:
            secret = secret_for(envelope, self.secrets)
            return bool(secret and verify_envelope(envelope, secret))

        return verify

    # ---------- inbound receipts ----------

    def _stored_envelopes(self) -> list[dict[str, Any]]:
        return [
            copy.deepcopy(self.state["receipts_by_key"][key]["envelope"])
            for key in self.state["receipt_order"]
        ]

    def receive_receipt(
        self,
        receipt: dict[str, Any],
        *,
        notify_late: bool = True,
        strict: bool = False,
    ) -> dict[str, Any]:
        if strict and self._verifier() is not None and not self._verifier()(receipt):
            raise CaptureError("receipt signature verification failed")

        key, body_hash = receipt_identity(receipt)
        envelope_hash = digest(receipt)
        with self._transaction():
            prior = self.state["receipts_by_key"].get(key)
            if prior is not None and prior["body_hash"] != body_hash:
                raise IdempotencyConflict(
                    f"receipt id/key {key} was reused with different content"
                )
            if prior is not None:
                exact_redelivery = prior.get("envelope_hash") == envelope_hash
                prior_bad_signature = (
                    prior.get("classification", {}).get("disposition") == "bad_signature"
                )
                if exact_redelivery or not prior_bad_signature:
                    self._audit("receipt_duplicate", key=key)
                    result = copy.deepcopy(prior["result"])
                    result.update(
                        {
                            "duplicate": True,
                            "accepted": prior["result"].get("accepted", False),
                            "duplicate_of_disposition": prior["result"].get("disposition"),
                            "status": self.ack_status(),
                            "current_status": self.ack_status(),
                        }
                    )
                    result["disposition"] = "duplicate"
                    return result

                # The same unsigned body was redelivered with a corrected
                # signature after a rejected first attempt.  A rejected envelope
                # had no external effect, so it is safe to reprocess once.
                self.state["receipts_by_key"].pop(key, None)
                if key in self.state["receipt_order"]:
                    self.state["receipt_order"].remove(key)
                self._audit("receipt_signature_retry", key=key)

            plan = self.plan
            classification = classify_receipt(
                receipt,
                self._command_for_receipt(receipt, plan),
                plan,
                verifier=self._verifier(),
            ) if role_of(receipt) in {c["role"] for c in plan["planned_commands"]} else {
                "valid": False,
                "disposition": "unknown_party",
                "reason": "回执角色不在当前命令轮次中",
            }
            accepted = bool(classification.get("valid"))
            record = {
                "key": key,
                "body_hash": body_hash,
                "envelope_hash": envelope_hash,
                "envelope": copy.deepcopy(receipt),
                "classification": classification,
                "received_seq": self._next_seq(),
                "accepted": accepted,
            }
            self.state["receipts_by_key"][key] = record
            self.state["receipt_order"].append(key)
            self._audit(
                "receipt_received",
                key=key,
                accepted=accepted,
                disposition=classification.get("disposition"),
                round=receipt.get("round") or receipt.get("round_no"),
            )

            result = {
                "duplicate": False,
                "accepted": accepted,
                "key": key,
                **classification,
                "status": self.ack_status(),
            }
            record["result"] = copy.deepcopy(result)

            had_completion = any(
                item["type"] == COMPLETION_TYPE for item in self.state["notifications"]
            )
            if had_completion and notify_late:
                self._notification_for_late_receipt(receipt, result)
            return copy.deepcopy(result)

    def _command_for_receipt(self, receipt: dict[str, Any], plan: dict[str, Any]):
        role = role_of(receipt)
        command_id = receipt.get("command_id")
        commands = plan["planned_commands"]
        if command_id:
            for command in commands:
                if command["command_id"] == command_id:
                    return command
        for command in commands:
            if command["role"] == role:
                return command
        # classify_receipt will report the role mismatch against first command if
        # no matching role exists; keep a concrete object for type consistency.
        return commands[0]

    def ack_status(self) -> str:
        if not self.state.get("plan"):
            return "pending"
        return evaluate(
            self.state["plan"],
            self._stored_envelopes(),
            verifier=self._verifier(),
        )["status"]

    def dispatch_evaluation(self) -> dict[str, Any]:
        return evaluate(
            self.plan,
            self._stored_envelopes(),
            verifier=self._verifier(),
        )

    # ---------- stationary confirmation / full closure ----------

    def confirm_stationary(
        self, confirmation: dict[str, Any], *, strict: bool = False
    ) -> dict[str, Any]:
        if strict and self._verifier() is not None and not self._verifier()(confirmation):
            raise CaptureError("stationary confirmation signature verification failed")
        key = (
            f"id:{confirmation.get('confirmation_id') or confirmation.get('id')}"
            if confirmation.get("confirmation_id") or confirmation.get("id")
            else f"hash:{digest(confirmation)}"
        )
        body_hash = digest({k: v for k, v in confirmation.items() if k != "signature"})
        with self._transaction():
            prior = self.state["confirmations"].get(key)
            if prior:
                if prior["body_hash"] != body_hash:
                    raise IdempotencyConflict("confirmation id reused with different content")
                result = copy.deepcopy(prior["result"])
                result["duplicate"] = True
                return result

            role = role_of(confirmation)
            plan = self.plan
            valid = True
            disposition = "stationary_confirmed"
            reason = "现场负责人确认机具静止"
            if role not in SITE_LEAD_ROLES:
                valid = False
                disposition = "wrong_party"
                reason = "只有现场负责人/负责人角色可确认机具静止"
            elif str(confirmation.get("incident_id")) != str(plan["incident_id"]):
                valid = False
                disposition = "wrong_incident"
                reason = "静止确认 incident_id 不匹配"
            elif not is_stationary_confirmation(confirmation):
                valid = False
                disposition = "not_stationary"
                reason = "确认内容不是机具静止"
            else:
                rr = confirmation.get("round") or confirmation.get("round_no")
                if rr is not None and coerce_round(rr) != plan["round"]:
                    valid = False
                    disposition = "stale_round"
                    reason = "静止确认来自旧轮次"
                root = confirmation.get("content_root") or confirmation.get("plan_root")
                if root is not None and root != plan["content_root"]:
                    valid = False
                    disposition = "wrong_content_root"
                    reason = "静止确认内容根不匹配"
                if self._verifier() is not None and not self._verifier()(confirmation):
                    valid = False
                    disposition = "bad_signature"
                    reason = "静止确认签名无效"

            record = {
                "key": key,
                "body_hash": body_hash,
                "envelope": copy.deepcopy(confirmation),
                "accepted": valid,
                "disposition": disposition,
                "seq": self._next_seq(),
                "result": None,
            }
            self.state["confirmations"][key] = record
            self.state["confirmation_order"].append(key)
            self._audit("stationary_confirmation", key=key, accepted=valid)
            result = {
                "duplicate": False,
                "accepted": valid,
                "disposition": disposition,
                "reason": reason,
                "closure": self.closure_status(),
            }
            record["result"] = copy.deepcopy(result)
            return copy.deepcopy(result)

    def _valid_stationary_confirmation(self) -> dict[str, Any] | None:
        plan = self.plan
        for key in self.state["confirmation_order"]:
            record = self.state["confirmations"][key]
            envelope = record["envelope"]
            if not record.get("accepted"):
                continue
            if str(envelope.get("incident_id")) != str(plan["incident_id"]):
                continue
            rr = envelope.get("round") or envelope.get("round_no")
            if rr is not None and coerce_round(rr) != plan["round"]:
                continue
            root = envelope.get("content_root") or envelope.get("plan_root")
            if root is not None and root != plan["content_root"]:
                continue
            return envelope
        return None

    def closure_status(self) -> dict[str, Any]:
        evaluation = evaluate(
            self.plan,
            self._stored_envelopes(),
            verifier=self._verifier(),
        )
        stationary = self._valid_stationary_confirmation()
        resolved = evaluation["status"] == "acknowledged" and stationary is not None
        return {
            "resolved": resolved,
            "status": "resolved" if resolved else "pending",
            "dispatch_status": evaluation["status"],
            "incident_id": self.plan["incident_id"],
            "round": self.plan["round"],
            "content_root": self.plan["content_root"],
            "stationary_confirmed": stationary is not None,
            "stationary_confirmation_id": (stationary or {}).get("confirmation_id")
            or (stationary or {}).get("id"),
            "requirements": evaluation["requirements"],
        }

    # ---------- outbound intents / watermarks ----------

    def controller_command(self) -> dict[str, Any]:
        for command in self.plan["planned_commands"]:
            if command["role"] in CONTROLLER_ROLES:
                return command
        raise CaptureError("current round has no controller command")

    def create_send_intent(
        self, role: str = "controller", *, action: str | None = None
    ) -> dict[str, Any]:
        command = None
        for candidate in self.plan["planned_commands"]:
            if candidate["role"] == role:
                command = candidate
                break
        if command is None:
            raise CaptureError(f"current round has no command for role {role!r}")
        if action is not None and action != command["action"]:
            raise IdempotencyConflict(
                "intent action does not match prepared content; prepare a new round instead"
            )

        token = command["token"]
        with self._transaction():
            existing = self.state["intents"].get(token)
            if existing is not None:
                if existing["action"] != command["action"]:
                    raise IdempotencyConflict("token already bound to another action")
                self._audit("send_intent_reused", token=token)
                result = copy.deepcopy(existing)
                result["duplicate"] = True
                return result

            intent = {
                "intent_id": token,
                "token": token,
                "idempotency_key": token,
                "incident_id": self.plan["incident_id"],
                "round": self.plan["round"],
                "role": command["role"],
                "party": command["party"],
                "command_id": command["command_id"],
                "action": command["action"],
                "target": command.get("target"),
                "command_root": command["command_root"],
                "content_root": self.plan["content_root"],
                "attempts": 0,
                "status": "durable_intent",
                "send_watermark": None,
                "created_seq": self._next_seq(),
            }
            self.state["intents"][token] = intent
            self._audit(
                "send_intent_created",
                token=token,
                round=intent["round"],
                action=intent["action"],
            )
            return copy.deepcopy(intent)

    register_intent = create_send_intent

    def _intent_for_token(self, token: str | None = None) -> dict[str, Any]:
        if token is None:
            token = self.controller_command()["token"]
        intent = self.state["intents"].get(token)
        if intent is None:
            # Recovery may legitimately arrive at a ledger where the deterministic
            # intent was not checkpointed.  Recreate it from the current plan;
            # the token remains the round/content-derived original token.
            command = self.controller_command()
            if token != command["token"]:
                raise IdempotencyConflict(f"unknown token {token!r} for current round")
            return self.create_send_intent("controller")
        return copy.deepcopy(intent)

    def begin_attempt(self, token: str) -> dict[str, Any]:
        with self._transaction():
            intent = self.state["intents"][token]
            intent["attempts"] += 1
            intent["status"] = "attempting"
            self._audit("send_attempt", token=token, attempt=intent["attempts"])
            return copy.deepcopy(intent)

    def mark_send_watermark(
        self, token: str, external_receipt: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        with self._transaction():
            if token not in self.state["intents"]:
                raise CaptureError(f"unknown send intent {token}")
            seq = self._next_seq()
            watermark = {
                "token": token,
                "seq": seq,
                "round": self.state["plan"]["round"],
                "external_receipt": copy.deepcopy(external_receipt),
            }
            self.state["watermarks"][token] = watermark
            self.state["intents"][token]["status"] = "send_watermarked"
            self.state["intents"][token]["send_watermark"] = seq
            self._audit("send_watermark", token=token, seq=seq)
            return copy.deepcopy(watermark)

    def dispatch(
        self, token: str | None = None, gateway: ControllerGateway | None = None
    ) -> dict[str, Any]:
        """Send or resend an intent using only its original token/action."""
        intent = self._intent_for_token(token)
        token = intent["token"]
        self.begin_attempt(token)
        latest_intent = self._intent_for_token(token)
        if gateway is None:
            # No external delivery has happened.  The durable intent/attempt is
            # retained, but writing a send watermark here would falsely claim
            # the post-send boundary was reached.
            return {
                "delivered": False,
                "watermarked": False,
                "reason": "offline_no_gateway",
                "token": token,
            }
        response = gateway.send(latest_intent)
        self.mark_send_watermark(token, response)
        response["watermarked"] = True
        return copy.deepcopy(response)

    resend_with_same_token = dispatch

    # ---------- controller query / authorized recovery paths ----------

    def query_controller(
        self,
        query_result: dict[str, Any] | None = None,
        *,
        gateway: ControllerGateway | None = None,
    ) -> dict[str, Any]:
        command = self.controller_command()
        token = command["token"]
        if query_result is not None:
            # An explicitly supplied signed controller response is evidence.
            # ``gateway`` may still be supplied later solely to execute a safe
            # resend in the not-landed branch.
            result = copy.deepcopy(query_result)
        elif gateway is not None:
            query = {
                "incident_id": self.plan["incident_id"],
                "round": self.plan["round"],
                "token": token,
                "party": "controller",
            }
            result = gateway.query(query)
        else:
            raise CaptureError("either query_result or gateway is required")

        if result.get("token") != token:
            raise IdempotencyConflict("controller query token is not the round-2 intent token")
        if str(result.get("incident_id")) != str(self.plan["incident_id"]):
            raise CaptureError("controller query incident_id mismatch")
        rr = result.get("round") or result.get("round_no")
        if rr is not None and coerce_round(rr) != self.plan["round"]:
            raise RoundConflict("controller query belongs to an old round")
        root = result.get("content_root")
        if root is not None and root not in {self.plan["content_root"], command["command_root"]}:
            raise RoundConflict("controller query content root mismatch")
        if self._verifier() is not None and not self._verifier()(result):
            raise CaptureError("controller query signature verification failed")

        landed = result.get("landed")
        stopped = is_stop_proven(result)
        normalized = {
            "token": token,
            "landed": bool(landed if landed is not None else stopped),
            "stopped": stopped,
            "state": result.get("state") or result.get("controller_state"),
            "result": copy.deepcopy(result),
        }
        with self._transaction():
            self.state["controller_queries"].append(
                {
                    "seq": self._next_seq(),
                    "token": token,
                    "landed": normalized["landed"],
                    "stopped": stopped,
                    "state": normalized["state"],
                }
            )
            self._audit(
                "controller_queried",
                token=token,
                landed=normalized["landed"],
                stopped=stopped,
            )
        return normalized

    def recover_from_controller_query(
        self,
        query_result: dict[str, Any] | None = None,
        *,
        gateway: ControllerGateway | None = None,
        stationary_confirmation: dict[str, Any] | None = None,
        publish: bool = True,
        resend_if_not_landed: bool = True,
    ) -> dict[str, Any]:
        """Branch into the two authorized recovery paths.

        Confirmed stop + stationary lead evidence -> backfill and publish.
        Proven not-landed -> safely resend with the original token and remain
        pending.
        """
        query = self.query_controller(query_result, gateway=gateway)
        token = query["token"]

        if query["stopped"]:
            proof = dict(query["result"])
            proof.setdefault("receipt_id", f"controller-query:{token[:16]}")
            proof.setdefault("party", "controller")
            proof.setdefault("role", "controller")
            proof.setdefault("incident_id", self.plan["incident_id"])
            proof.setdefault("round", self.plan["round"])
            proof.setdefault("content_root", self.plan["content_root"])
            proof.setdefault("token", token)
            if self.secrets is not None:
                secret = secret_for(proof, self.secrets)
                if secret:
                    proof = sign_envelope(proof, secret)

            # Commit the local recovery boundary atomically: watermark,
            # backfilled receipt, stationary witness, and completion publication.
            with self._transaction():
                self.mark_send_watermark(
                    token, {"recovered_from": "controller_query", **query}
                )
                receipt_result = self.receive_receipt(proof)
                stationary_result = None
                if stationary_confirmation is not None:
                    stationary_result = self.confirm_stationary(
                        stationary_confirmation
                    )
                completion = self.publish_completion() if publish else None

            return {
                "path": "controller_confirmed",
                "query": query,
                "receipt": receipt_result,
                "stationary": stationary_result,
                "completion": completion,
                "closure": self.closure_status(),
            }

        if query["landed"]:
            # Landed but not yet stopped is still not closure.  Do not invent a
            # stop receipt; callers wait for the independent stop proof.
            return {
                "path": "landed_pending_stop",
                "query": query,
                "dispatch": None,
                "completion": None,
                "closure": self.closure_status(),
            }

        dispatch_result = None
        if resend_if_not_landed:
            # With no gateway this records another durable attempt/outbox state
            # without falsely writing the post-send watermark.
            dispatch_result = self.resend_with_same_token(token, gateway)
        with self._transaction():
            self.state["recovery_actions"].append(
                {"seq": self._next_seq(), "path": "safe_resend", "token": token}
            )
            self._audit("recovery_safe_resend", token=token)
        return {
            "path": "not_landed_safe_resend",
            "query": query,
            "dispatch": dispatch_result,
            "completion": None,
            "closure": self.closure_status(),
        }

    # ---------- notifications ----------

    def _active_completion(self) -> dict[str, Any] | None:
        completions = [n for n in self.state["notifications"] if n["type"] == COMPLETION_TYPE]
        if not completions:
            return None
        latest_completion = completions[-1]
        retractions = [
            n
            for n in self.state["notifications"]
            if n["type"] == CORRECTION_TYPE
            and n["seq"] > latest_completion["seq"]
            and n.get("corrects") == latest_completion["notification_id"]
            and n.get("current_resolution") is False
        ]
        if retractions:
            # A retraction permanently closes the original publication event.
            # Re-resolution must publish a new notification, never silently
            # revive or overwrite the immutable original.
            return None
        return latest_completion

    def publish_completion(
        self,
        *,
        notification_id: str | None = None,
        recipient_role: str = "field_owner",
    ) -> dict[str, Any]:
        closure = self.closure_status()
        if not closure["resolved"]:
            raise IncidentNotResolved(
                "refusing completion notification: "
                + json.dumps(closure, ensure_ascii=False, sort_keys=True)
            )
        with self._transaction():
            active = self._active_completion()
            if active is not None and active.get("content_root") == closure["content_root"]:
                result = copy.deepcopy(active)
                result["duplicate"] = True
                return result

            seq = self._next_seq()
            notification = {
                "notification_id": notification_id or f"N-{seq}",
                "seq": seq,
                "type": COMPLETION_TYPE,
                "incident_id": closure["incident_id"],
                "round": closure["round"],
                "content_root": closure["content_root"],
                "recipient_role": recipient_role,
                "title": "险情处置完成",
                "body": "运行机具已收到撤离指令，现场控制器已停机，并已确认机具静止。",
                "current_resolution": True,
                "evidence": {
                    "requirements": closure["requirements"],
                    "stationary_confirmation_id": closure["stationary_confirmation_id"],
                },
            }
            self.state["notifications"].append(notification)
            self._audit(
                "completion_published",
                notification_id=notification["notification_id"],
                round=closure["round"],
                content_root=closure["content_root"],
            )
            result = copy.deepcopy(notification)
            result["duplicate"] = False
            return result

    def publish_correction(
        self,
        *,
        corrects: str | None = None,
        kind: str = "correction",
        reason: str,
        source_receipt: dict[str, Any] | None = None,
        current_resolution: bool | None = None,
        notification_id: str | None = None,
    ) -> dict[str, Any]:
        with self._transaction():
            target = corrects
            if target is None:
                completions = [
                    n for n in self.state["notifications"] if n["type"] == COMPLETION_TYPE
                ]
                if not completions:
                    raise CaptureError("there is no completion notification to correct")
                target = completions[-1]["notification_id"]
            target_notification = next(
                (n for n in self.state["notifications"] if n["notification_id"] == target),
                None,
            )
            if target_notification is None:
                raise CaptureError(f"notification {target} does not exist")

            closure = self.closure_status() if self.state.get("plan") else None
            if current_resolution is None:
                current_resolution = bool(closure and closure["resolved"])
            seq = self._next_seq()
            correction = {
                "notification_id": notification_id or f"N-{seq}",
                "seq": seq,
                "type": CORRECTION_TYPE,
                "correction_kind": kind,
                "corrects": target,
                "incident_id": target_notification["incident_id"],
                "round": (closure or {}).get("round", target_notification.get("round")),
                "content_root": (closure or {}).get(
                    "content_root", target_notification.get("content_root")
                ),
                "title": "险情处置完成通知更正/追踪",
                "reason": reason,
                "source_receipt_id": (source_receipt or {}).get("receipt_id")
                or (source_receipt or {}).get("id"),
                "current_resolution": current_resolution,
                "closure": closure,
            }
            self.state["notifications"].append(correction)
            self._audit(
                "correction_published",
                notification_id=correction["notification_id"],
                corrects=target,
                kind=kind,
                current_resolution=current_resolution,
            )
            return copy.deepcopy(correction)

    def _notification_for_late_receipt(
        self, receipt: dict[str, Any], processing_result: dict[str, Any]
    ) -> dict[str, Any] | None:
        completions = [n for n in self.state["notifications"] if n["type"] == COMPLETION_TYPE]
        if not completions:
            return None
        target = completions[-1]["notification_id"]
        closure = self.closure_status()
        accepted = processing_result.get("accepted")
        disposition = processing_result.get("disposition")

        if accepted and not closure["resolved"]:
            kind = "revocation_or_contradiction"
            reason = "迟到/撤销回执与已发布完成状态冲突，原完成通知不可改写，发布纠正通知。"
            current_resolution = False
        elif accepted:
            kind = "late_receipt_recorded"
            reason = "完成通知发布后收到非重复迟到回执；原文保留，另发可追踪补充/纠正通知。"
            current_resolution = True
        else:
            kind = "late_receipt_rejected"
            reason = (
                f"迟到回执被栅栏拒绝（{disposition}）；原完成通知不静默改写，"
                "另发追踪通知说明其未填补当前缺口。"
            )
            current_resolution = closure["resolved"]

        seq = self._next_seq()
        correction = {
            "notification_id": f"N-{seq}",
            "seq": seq,
            "type": CORRECTION_TYPE,
            "correction_kind": kind,
            "corrects": target,
            "incident_id": self.plan["incident_id"],
            "round": self.plan["round"],
            "content_root": self.plan["content_root"],
            "title": "险情处置完成通知迟到回执追踪",
            "reason": reason,
            "disposition": disposition,
            "source_receipt_id": receipt.get("receipt_id") or receipt.get("id"),
            "current_resolution": current_resolution,
            "closure": closure,
        }
        self.state["notifications"].append(correction)
        self._audit("late_receipt_notice", notification_id=correction["notification_id"])
        return correction

    # ---------- read models ----------

    def notifications(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self.state["notifications"])

    def corrections(self) -> list[dict[str, Any]]:
        return [n for n in self.notifications() if n["type"] == CORRECTION_TYPE]

    def audit_trail(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self.state["audit"])


# Descriptive aliases for callers/tests that name the ledger differently.
Capture = IncidentCapture
CaptureLedger = IncidentCapture
RecoveryLedger = IncidentCapture
IncidentRecovery = IncidentCapture

# Verb-oriented aliases for alternate caller vocabulary.
IncidentCapture.recover = IncidentCapture.recover_from_controller_query
IncidentCapture.add_ack = IncidentCapture.receive_receipt
IncidentCapture.receive_ack = IncidentCapture.receive_receipt
IncidentCapture.request_completion = IncidentCapture.publish_completion
