import copy
import json
import tempfile
import unittest
from pathlib import Path

from fieldmachine.dispatch_ack import detail_status, status
from fieldmachine.incident import prepare
from fieldmachine.incident_capture import (
    ControllerGateway,
    IdempotencyConflict,
    IncidentCapture,
    IncidentNotResolved,
    verify_envelope,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "incident-recovery.json"


def load_fixture():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def crashed_before_watermark(fixture):
    """Build the shared durable local state at the specified power-loss point."""
    capture = IncidentCapture(secrets=fixture["secrets"])
    round_one = fixture["rounds"]["one"]
    capture.prepare_round(round_one["incident"])

    driver_result = capture.receive_receipt(round_one["driver_receipt"])
    if not driver_result["accepted"] or capture.ack_status() != "acknowledged":
        raise AssertionError("round 1 fixture did not start acknowledged")

    capture.prepare_round(fixture["rounds"]["two"]["incident"])
    if capture.ack_status() != "pending":
        raise AssertionError("round 2 expansion must regress status to pending")

    intent = capture.create_send_intent("controller")
    if intent["token"] != fixture["intent"]["token"]:
        raise AssertionError("prepared controller token does not match fixture")
    capture.begin_attempt(intent["token"])

    snapshot = capture.snapshot()
    if snapshot["watermarks"] or intent["token"] not in snapshot["intents"]:
        raise AssertionError("power-loss checkpoint is not at intent-before-watermark")
    return snapshot, intent


class IncidentPlanningTest(unittest.TestCase):
    def test_prepare_round_content_roots_and_round_tokens(self):
        fixture = load_fixture()
        one = prepare(fixture["rounds"]["one"]["incident"])
        two = prepare(fixture["rounds"]["two"]["incident"])

        self.assertEqual(one["round"], 1)
        self.assertEqual(one["required_roles"], ["driver"])
        self.assertEqual(two["required_roles"], ["driver", "controller"])
        self.assertEqual(two["content_root"], fixture["intent"]["content_root"])

        driver_one = one["planned_commands"][0]
        driver_two = two["planned_commands"][0]
        controller_two = two["planned_commands"][1]

        # Adding controller changes the global root and round token, while the
        # unchanged driver instruction keeps its stable semantic command root.
        self.assertEqual(driver_one["command_root"], driver_two["command_root"])
        self.assertNotEqual(driver_one["token"], driver_two["token"])
        self.assertEqual(controller_two["token"], fixture["intent"]["token"])
        self.assertEqual(controller_two["command_root"], fixture["intent"]["command_root"])
        self.assertTrue(controller_two["requires_stop_proof"])

    def test_changed_driver_content_breaks_old_content_root(self):
        fixture = load_fixture()
        changed = {
            "incident_id": "I1",
            "round": 2,
            "commands": [
                {"role": "driver", "action": "evacuate", "target": "M-CHANGED"},
                "controller",
            ],
        }
        evaluation = detail_status(changed, fixture["acknowledgements"])
        roles = {item["role"]: item for item in evaluation["requirements"]}
        self.assertEqual(evaluation["status"], "pending")
        self.assertFalse(roles["driver"]["satisfied"])
        self.assertIn(roles["driver"]["disposition"], {"stale_round", "wrong_content_root"})


class DispatchStatusFenceTest(unittest.TestCase):
    def test_status_regresses_until_round_two_stop_proof(self):
        fixture = load_fixture()
        round_one = fixture["rounds"]["one"]["incident"]
        round_two = fixture["rounds"]["two"]["incident"]
        driver_r1 = fixture["rounds"]["one"]["driver_receipt"]
        controller_stop = fixture["receipts"]["controller_stop"]

        self.assertEqual(status(round_one, [driver_r1]), "acknowledged")
        self.assertEqual(status(round_two, [driver_r1]), "pending")

        evaluation = detail_status(round_two, [driver_r1])
        requirements = {item["role"]: item for item in evaluation["requirements"]}
        self.assertTrue(requirements["driver"]["satisfied"])
        self.assertTrue(requirements["driver"]["carried_forward"])
        self.assertFalse(requirements["controller"]["satisfied"])
        self.assertEqual(requirements["controller"]["disposition"], "missing")

        # The same round-2 independent stop proof closes the new controller gap.
        self.assertEqual(status(round_two, [driver_r1, controller_stop]), "acknowledged")

    def test_strict_round_fence_rejects_old_driver_round(self):
        fixture = load_fixture()
        evaluation = detail_status(
            fixture["incident"],
            fixture["acknowledgements"],
            allow_carry_forward=False,
        )
        driver = next(item for item in evaluation["requirements"] if item["role"] == "driver")
        self.assertFalse(driver["satisfied"])
        self.assertEqual(driver["disposition"], "stale_round")
        self.assertEqual(evaluation["status"], "pending")

    def test_controller_ack_and_driver_claim_are_not_stop_evidence(self):
        fixture = load_fixture()
        token = fixture["intent"]["token"]
        root = fixture["intent"]["content_root"]
        command_root = fixture["intent"]["command_root"]
        driver_r1 = fixture["rounds"]["one"]["driver_receipt"]

        plain_controller_ack = {
            "receipt_id": "ACK-C-PLAIN",
            "incident_id": "I1",
            "round": 2,
            "party": "controller",
            "token": token,
            "content_root": root,
            "command_root": command_root,
            "acknowledged": True,
        }
        self.assertEqual(
            status(fixture["incident"], [driver_r1, plain_controller_ack]),
            "pending",
        )

        driver_sourced_stop = {
            "receipt_id": "ACK-C-DRIVER-SOURCE",
            "incident_id": "I1",
            "round": 2,
            "party": "controller",
            "source": "driver",
            "state": "stopped",
            "stopped": True,
            "token": token,
            "content_root": root,
            "command_root": command_root,
        }
        evaluation = detail_status(
            fixture["incident"], [driver_r1, driver_sourced_stop]
        )
        controller = next(
            item for item in evaluation["requirements"] if item["role"] == "controller"
        )
        self.assertFalse(controller["satisfied"])

    def test_wrong_round_token_or_content_root_does_not_close(self):
        fixture = load_fixture()
        bad_token = copy.deepcopy(fixture["receipts"]["controller_stop"])
        bad_token["receipt_id"] = "ACK-C-BAD-TOKEN"
        bad_token["token"] = "idem-forged-new-action"
        self.assertEqual(
            status(
                fixture["incident"],
                [fixture["rounds"]["one"]["driver_receipt"], bad_token],
            ),
            "pending",
        )

        bad_root = copy.deepcopy(fixture["receipts"]["controller_stop"])
        bad_root["receipt_id"] = "ACK-C-BAD-ROOT"
        bad_root["content_root"] = "0" * 64
        bad_root["command_root"] = "1" * 64
        self.assertEqual(
            status(
                fixture["incident"],
                [fixture["rounds"]["one"]["driver_receipt"], bad_root],
            ),
            "pending",
        )


    def test_contradictory_controller_states_without_order_fence_fail_closed(self):
        fixture = load_fixture()
        stop = copy.deepcopy(fixture["receipts"]["controller_stop"])
        running = copy.deepcopy(fixture["receipts"]["controller_revocation"])
        for envelope in (stop, running):
            envelope.pop("state_seq", None)

        # Arrival/list order must not silently choose physical state when there
        # is no source sequence/timestamp telling which controller state is newer.
        for receipts in (
            [fixture["rounds"]["one"]["driver_receipt"], stop, running],
            [fixture["rounds"]["one"]["driver_receipt"], running, stop],
        ):
            evaluation = detail_status(fixture["incident"], receipts)
            controller = next(
                item
                for item in evaluation["requirements"]
                if item["role"] == "controller"
            )
            self.assertEqual(evaluation["status"], "pending")
            self.assertEqual(controller["disposition"], "conflicting_state_evidence")


class SignatureAndDuplicateTest(unittest.TestCase):
    def test_fixture_signatures_verify_and_tampering_is_rejected(self):
        fixture = load_fixture()
        for group in [
            [fixture["rounds"]["one"]["driver_receipt"]],
            [fixture["receipts"]["controller_stop"]],
            [fixture["recovery_paths"]["controller_confirmed"]["stationary_confirmation"]],
        ]:
            envelope = group[0]
            secret = fixture["secrets"][envelope["party"]]
            self.assertTrue(verify_envelope(envelope, secret))

        capture = IncidentCapture(secrets=fixture["secrets"])
        capture.prepare_round(fixture["rounds"]["two"]["incident"])
        tampered = copy.deepcopy(fixture["receipts"]["controller_stop"])
        tampered["state"] = "running"
        result = capture.receive_receipt(tampered)
        self.assertFalse(result["accepted"])
        self.assertEqual(result["disposition"], "bad_signature")

    def test_duplicate_receipt_id_with_changed_body_conflicts(self):
        fixture = load_fixture()
        capture = IncidentCapture(secrets=fixture["secrets"])
        capture.prepare_round(fixture["rounds"]["two"]["incident"])
        first = copy.deepcopy(fixture["receipts"]["controller_stop"])
        capture.receive_receipt(first)

        changed = copy.deepcopy(first)
        changed["stopped"] = False
        changed["state"] = "running"
        # Re-sign with the correct key so this is an id reuse/content conflict,
        # not a signature rejection.
        from fieldmachine.incident_capture import sign_envelope

        changed = sign_envelope(changed, fixture["secrets"]["controller"])
        with self.assertRaises(IdempotencyConflict):
            capture.receive_receipt(changed)

    def test_same_body_with_corrected_signature_can_reprocess_rejected_delivery(self):
        fixture = load_fixture()
        capture = IncidentCapture(secrets=fixture["secrets"])
        capture.prepare_round(fixture["rounds"]["two"]["incident"])

        bad_signature = copy.deepcopy(fixture["receipts"]["controller_stop"])
        bad_signature["signature"] = "0" * 64
        rejected = capture.receive_receipt(bad_signature)
        self.assertFalse(rejected["accepted"])
        self.assertEqual(rejected["disposition"], "bad_signature")

        corrected = capture.receive_receipt(
            copy.deepcopy(fixture["receipts"]["controller_stop"])
        )
        self.assertFalse(corrected["duplicate"])
        self.assertTrue(corrected["accepted"])
        self.assertEqual(corrected["disposition"], "current_stop_proof")


class TransactionBoundaryTest(unittest.TestCase):
    def test_failed_notification_leaves_transactional_state_unchanged(self):
        fixture = load_fixture()
        capture = IncidentCapture(secrets=fixture["secrets"])
        capture.prepare_round(fixture["rounds"]["two"]["incident"])
        before = capture.snapshot()

        with self.assertRaises(IncidentNotResolved):
            capture.publish_completion()

        self.assertEqual(capture.snapshot(), before)
        self.assertEqual(capture.notifications(), [])


class RecoveryPathTest(unittest.TestCase):
    def test_power_loss_checkpoint_has_intent_but_no_watermark(self):
        fixture = load_fixture()
        snapshot, intent = crashed_before_watermark(fixture)
        self.assertEqual(intent["action"], "stop")
        self.assertEqual(intent["round"], 2)
        self.assertEqual(snapshot["watermarks"], {})
        self.assertGreaterEqual(snapshot["intents"][intent["token"]]["attempts"], 1)
        events = [entry["event"] for entry in snapshot["audit"]]
        self.assertIn("send_intent_created", events)
        self.assertIn("send_attempt", events)
        self.assertNotIn("send_watermark", events)

    def test_offline_dispatch_without_gateway_does_not_write_watermark(self):
        fixture = load_fixture()
        snapshot, intent = crashed_before_watermark(fixture)
        capture = IncidentCapture.restore(snapshot, secrets=fixture["secrets"])

        result = capture.dispatch(intent["token"])

        self.assertFalse(result["delivered"])
        self.assertFalse(result["watermarked"])
        self.assertEqual(capture.state["watermarks"], {})
        self.assertEqual(capture.state["intents"][intent["token"]]["status"], "attempting")

    def test_explicit_signed_query_takes_precedence_over_empty_gateway(self):
        fixture = load_fixture()
        snapshot, _ = crashed_before_watermark(fixture)
        capture = IncidentCapture.restore(snapshot, secrets=fixture["secrets"])
        empty_gateway = ControllerGateway(secret=fixture["secrets"]["controller"])

        result = capture.recover_from_controller_query(
            query_result=fixture["recovery_paths"]["controller_confirmed"][
                "controller_query"
            ],
            gateway=empty_gateway,
            stationary_confirmation=fixture["recovery_paths"][
                "controller_confirmed"
            ]["stationary_confirmation"],
        )

        self.assertEqual(result["path"], "controller_confirmed")
        self.assertTrue(result["closure"]["resolved"])
        self.assertIsNotNone(result["completion"])
        self.assertEqual(empty_gateway.action_log, [])

    def test_invalid_stationary_witness_rolls_back_confirmed_recovery(self):
        fixture = load_fixture()
        snapshot, intent = crashed_before_watermark(fixture)
        capture = IncidentCapture.restore(snapshot, secrets=fixture["secrets"])
        gateway = ControllerGateway(secret=fixture["secrets"]["controller"])
        gateway.seed_stopped(intent["token"], incident_id="I1", round=2)
        invalid_lead = {
            "confirmation_id": "LEAD-BAD",
            "incident_id": "I1",
            "round": 2,
            "party": "site_lead",
            "machines_stationary": False,
        }

        with self.assertRaises(IncidentNotResolved):
            capture.recover_from_controller_query(
                gateway=gateway, stationary_confirmation=invalid_lead
            )

        self.assertNotIn(intent["token"], capture.state["watermarks"])
        self.assertEqual(len(capture._stored_envelopes()), 1)  # only r1 driver
        self.assertEqual(capture.notifications(), [])

    def test_identical_checkpoint_supports_confirmed_and_not_landed_paths(self):
        fixture = load_fixture()
        snapshot, intent = crashed_before_watermark(fixture)
        token = intent["token"]

        # Serialize the committed local database/state at the power-loss point.
        # Both recovery branches restart from this same offline local input.
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "I1-incident-state.json"
            state_path.write_text(json.dumps(snapshot), encoding="utf-8")
            durable_input = state_path.read_text(encoding="utf-8")

            # ---- Path A: controller query proves same-token round-2 stop ----
            confirmed = IncidentCapture.restore(
                durable_input, secrets=fixture["secrets"]
            )
            old_redelivery = confirmed.receive_receipt(
                fixture["rounds"]["two"]["stale_driver_redelivery"]
            )
            self.assertTrue(old_redelivery["duplicate"])
            self.assertEqual(old_redelivery["status"], "pending")

            gateway_a = ControllerGateway(secret=fixture["secrets"]["controller"])
            gateway_a.seed_stopped(
                token,
                incident_id="I1",
                round=2,
                content_root=fixture["intent"]["content_root"],
                command_root=fixture["intent"]["command_root"],
            )
            path_a = confirmed.recover_from_controller_query(
                gateway=gateway_a,
                stationary_confirmation=fixture["recovery_paths"][
                    "controller_confirmed"
                ]["stationary_confirmation"],
            )
            self.assertEqual(path_a["path"], "controller_confirmed")
            self.assertTrue(path_a["closure"]["resolved"])
            self.assertEqual(path_a["closure"]["status"], "resolved")
            self.assertIsNotNone(path_a["completion"])
            self.assertEqual(len(confirmed.notifications()), 1)
            self.assertEqual(
                confirmed.notifications()[0]["type"],
                "incident_resolution_completed",
            )
            self.assertIn(token, confirmed.state["watermarks"])

            # ---- Path B: controller proves non-landing; same token resends ----
            pending = IncidentCapture.restore(
                durable_input, secrets=fixture["secrets"]
            )
            gateway_b = ControllerGateway(secret=fixture["secrets"]["controller"])
            path_b = pending.recover_from_controller_query(
                query_result=fixture["recovery_paths"]["not_landed"][
                    "controller_query"
                ],
                gateway=gateway_b,
            )
            self.assertEqual(path_b["path"], "not_landed_safe_resend")
            self.assertFalse(path_b["closure"]["resolved"])
            self.assertEqual(pending.ack_status(), "pending")
            self.assertIsNone(path_b["completion"])
            self.assertEqual(len(pending.notifications()), 0)
            self.assertEqual(len(gateway_b.action_log), 1)
            self.assertEqual(gateway_b.action_log[0]["token"], token)
            self.assertEqual(gateway_b.action_log[0]["action"], "stop")

            # Retrying the resend cannot create a second external action.
            duplicate_send = pending.resend_with_same_token(token, gateway_b)
            self.assertTrue(duplicate_send["duplicate"])
            self.assertEqual(len(gateway_b.action_log), 1)
            self.assertEqual(gateway_b.action_log[0]["action"], "stop")

            # Same token cannot be reused for contradictory resume.
            with self.assertRaises(IdempotencyConflict):
                gateway_b.send(
                    {
                        "token": token,
                        "action": "resume",
                        "command_root": fixture["intent"]["command_root"],
                    }
                )
            with self.assertRaises(IncidentNotResolved):
                pending.publish_completion()

    def test_confirmed_path_duplicate_query_and_completion_are_idempotent(self):
        fixture = load_fixture()
        snapshot, _ = crashed_before_watermark(fixture)
        capture = IncidentCapture.restore(snapshot, secrets=fixture["secrets"])
        gateway = ControllerGateway(secret=fixture["secrets"]["controller"])
        token = fixture["intent"]["token"]
        gateway.seed_stopped(token, incident_id="I1", round=2)
        lead = fixture["recovery_paths"]["controller_confirmed"]["stationary_confirmation"]

        first = capture.recover_from_controller_query(
            gateway=gateway, stationary_confirmation=lead
        )
        second = capture.recover_from_controller_query(
            gateway=gateway, stationary_confirmation=lead
        )
        self.assertFalse(first["completion"].get("duplicate", False))
        self.assertTrue(second["receipt"]["duplicate"])
        self.assertTrue(second["stationary"]["duplicate"])
        self.assertTrue(second["completion"]["duplicate"])
        self.assertEqual(len(capture.notifications()), 1)

        repeated_completion = capture.publish_completion()
        self.assertTrue(repeated_completion["duplicate"])
        self.assertEqual(len(capture.notifications()), 1)

    def test_late_consistent_receipt_and_revocation_leave_corrections(self):
        fixture = load_fixture()
        snapshot, _ = crashed_before_watermark(fixture)
        capture = IncidentCapture.restore(snapshot, secrets=fixture["secrets"])
        gateway = ControllerGateway(secret=fixture["secrets"]["controller"])
        gateway.seed_stopped(fixture["intent"]["token"], incident_id="I1", round=2)
        lead = fixture["recovery_paths"]["controller_confirmed"]["stationary_confirmation"]
        capture.recover_from_controller_query(
            gateway=gateway, stationary_confirmation=lead
        )

        original = copy.deepcopy(capture.notifications()[0])
        self.assertEqual(original["type"], "incident_resolution_completed")

        # A new, non-duplicate consistent stop receipt arrives after publication.
        late_stop = capture.receive_receipt(fixture["receipts"]["controller_stop"])
        self.assertTrue(late_stop["accepted"])
        corrections = capture.corrections()
        self.assertEqual(len(corrections), 1)
        self.assertEqual(corrections[0]["correction_kind"], "late_receipt_recorded")
        self.assertEqual(corrections[0]["corrects"], original["notification_id"])
        self.assertTrue(corrections[0]["current_resolution"])

        # A current-round signed revocation reopens the incident and corrects it.
        revocation_result = capture.receive_receipt(
            fixture["receipts"]["controller_revocation"]
        )
        self.assertTrue(revocation_result["accepted"])
        self.assertEqual(revocation_result["disposition"], "stop_revoked_or_running")
        self.assertEqual(capture.ack_status(), "pending")
        self.assertFalse(capture.closure_status()["resolved"])

        corrections = capture.corrections()
        self.assertEqual(len(corrections), 2)
        revocation_notice = corrections[-1]
        self.assertEqual(
            revocation_notice["correction_kind"], "revocation_or_contradiction"
        )
        self.assertEqual(revocation_notice["corrects"], original["notification_id"])
        self.assertFalse(revocation_notice["current_resolution"])
        self.assertEqual(
            revocation_notice["source_receipt_id"], "ACK-C-I1-R2-REVOKE"
        )

        # The published completion is append-only and not silently rewritten.
        self.assertEqual(capture.notifications()[0], original)
        with self.assertRaises(IncidentNotResolved):
            capture.publish_completion()

        # Repeating the revocation is deduplicated and adds no second correction.
        repeat = capture.receive_receipt(fixture["receipts"]["controller_revocation"])
        self.assertTrue(repeat["duplicate"])
        self.assertEqual(len(capture.corrections()), 2)

    def test_out_of_order_old_stop_does_not_override_newer_revocation(self):
        from fieldmachine.incident_capture import sign_envelope

        fixture = load_fixture()
        snapshot, _ = crashed_before_watermark(fixture)
        capture = IncidentCapture.restore(snapshot, secrets=fixture["secrets"])
        gateway = ControllerGateway(secret=fixture["secrets"]["controller"])
        gateway.seed_stopped(fixture["intent"]["token"], incident_id="I1", round=2)
        lead = fixture["recovery_paths"]["controller_confirmed"][
            "stationary_confirmation"
        ]
        capture.recover_from_controller_query(
            gateway=gateway, stationary_confirmation=lead
        )
        original_id = capture.notifications()[0]["notification_id"]

        capture.receive_receipt(fixture["receipts"]["controller_revocation"])
        self.assertEqual(capture.ack_status(), "pending")

        # Network reorders an older state_seq=2 stop so it arrives after the
        # state_seq=3 revocation.  It must not falsely re-resolve the incident.
        old_late_stop = capture.receive_receipt(fixture["receipts"]["controller_stop"])
        self.assertTrue(old_late_stop["accepted"])
        evaluation = capture.dispatch_evaluation()
        controller = next(
            item for item in evaluation["requirements"] if item["role"] == "controller"
        )
        self.assertFalse(controller["satisfied"])
        self.assertEqual(controller["disposition"], "stop_revoked_or_running")
        self.assertEqual(capture.ack_status(), "pending")
        self.assertFalse(capture.closure_status()["resolved"])

        # A genuinely newer state_seq=4 stop can restore closure and requires a
        # new completion event rather than reviving/editing the retracted N1.
        newer_stop = copy.deepcopy(fixture["receipts"]["controller_stop"])
        newer_stop["receipt_id"] = "ACK-C-I1-R2-RE-STOP"
        newer_stop["state_seq"] = 4
        newer_stop = sign_envelope(newer_stop, fixture["secrets"]["controller"])
        capture.receive_receipt(newer_stop)
        self.assertTrue(capture.closure_status()["resolved"])
        new_completion = capture.publish_completion()
        self.assertFalse(new_completion.get("duplicate", False))
        completion_ids = [
            item["notification_id"]
            for item in capture.notifications()
            if item["type"] == "incident_resolution_completed"
        ]
        self.assertEqual(len(completion_ids), 2)
        self.assertIn(original_id, completion_ids)
        self.assertIn(new_completion["notification_id"], completion_ids)


if __name__ == "__main__":
    unittest.main()
