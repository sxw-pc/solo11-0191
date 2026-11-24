import copy
import json
import random
import tempfile
import unittest
from pathlib import Path

from fieldmachine.operator_pay import (
    coaching_minutes,
    disputed_minutes,
    frozen_minutes,
    payable_minutes,
    ride_minutes,
)
from fieldmachine.operator_shifts import reconcile, shifts, raw_shifts
from fieldmachine.operator_capture import (
    BatchLockedError,
    OperatorCapture,
    WageLedger,
    card_fingerprint,
)

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "fixtures" / "operator-handoff.json"
OK_FIX = ROOT / "fixtures" / "operator-ok.json"
FIXTURE = json.loads(FIX.read_text(encoding="utf-8"))


def base_events():
    return copy.deepcopy(FIXTURE["events"])


def handoff_570():
    return copy.deepcopy(FIXTURE["handoff_570"])


def handoff_630():
    return copy.deepcopy(FIXTURE["handoff_630"])


def coaching():
    return copy.deepcopy(FIXTURE["coaching_570_630"])


class SeedCompatibilityTest(unittest.TestCase):
    def test_operator_ok_fixture_still_pays_240(self):
        data = json.loads(OK_FIX.read_text(encoding="utf-8"))
        self.assertEqual(payable_minutes(data["events"]), {"driver-1": 240})

    def test_raw_shifts_keep_operator_grouping(self):
        data = json.loads(OK_FIX.read_text(encoding="utf-8"))
        grouped = raw_shifts(data["events"])
        self.assertEqual(len(grouped["driver-1"]), 2)

    def test_adjudicated_shifts_keep_seed_endpoints(self):
        data = json.loads(OK_FIX.read_text(encoding="utf-8"))
        op_shifts = shifts(data["events"])
        self.assertEqual([(r["start_minute"], r["end_minute"]) for r in op_shifts["driver-1"]],
                         [(480, 600), (600, 720)])
        self.assertEqual(op_shifts["driver-1"].minutes, 240)


class FrozenConflictTest(unittest.TestCase):
    def test_fixture_documents_sixty_frozen_minutes(self):
        result = reconcile(base_events())
        self.assertEqual(result["payable_minutes"], {"driver-1": 180})
        self.assertEqual(result["frozen_minutes"], 60)
        self.assertEqual(frozen_minutes(base_events()), 60)
        self.assertEqual(int(disputed_minutes(base_events())), 60)
        self.assertEqual(disputed_minutes(base_events()),
                         {"driver-1": 60, "driver-2": 60})

    def test_each_operator_sees_its_claim_frozen(self):
        op_shifts = shifts(base_events())
        d1 = [(r["start_minute"], r["end_minute"], r["kind"]) for r in op_shifts["driver-1"]]
        self.assertIn((480, 570, "work"), d1)
        self.assertIn((570, 600, "disputed"), d1)
        self.assertIn((600, 630, "disputed"), d1)
        self.assertIn((630, 720, "work"), d1)
        merged_d1 = [(r["start_minute"], r["end_minute"], r["kind"])
                     for r in op_shifts["driver-1"].merged]
        self.assertIn((570, 630, "disputed"), merged_d1)
        d2 = [(r["start_minute"], r["end_minute"], r["kind"]) for r in op_shifts["driver-2"]]
        self.assertEqual(d2, [(570, 600, "disputed"), (600, 630, "disputed")])
        self.assertEqual(op_shifts["driver-2"].minutes, 0)
        self.assertEqual(op_shifts["driver-2"].disputed_minutes, 60)

    def test_terminal_alone_does_not_attribute_driver(self):
        without_terminal = [e for e in base_events() if e["kind"] != "terminal"]
        self.assertEqual(payable_minutes(without_terminal), {"driver-1": 180})
        result = reconcile(base_events())
        disputed = [s for m in result["machines"].values()
                     for s in m["segments"] if s["status"] == "disputed"]
        self.assertTrue(disputed)
        self.assertTrue(all(s["terminal_verified"] for s in disputed))
        self.assertTrue(all(s["operator_id"] is None for s in disputed))

    def test_unsigned_handoff_cannot_break_tie(self):
        events = base_events() + [copy.deepcopy(FIXTURE["unsigned_handoff_570"])]
        self.assertEqual(payable_minutes(events), {"driver-1": 180})
        self.assertEqual(frozen_minutes(events), 60)

    def test_upload_order_never_decides_ownership(self):
        evidence = [handoff_570(), copy.deepcopy(FIXTURE["unsigned_handoff_570"])]
        reference = payable_minutes(base_events() + evidence)
        for seed in range(12):
            events = base_events() + evidence
            random.Random(seed).shuffle(events)
            self.assertEqual(payable_minutes(events), reference)
            self.assertEqual(frozen_minutes(events), 0)
        frozen_reference = payable_minutes(base_events() + [FIXTURE["unsigned_handoff_570"]])
        for seed in range(5):
            events = base_events() + [copy.deepcopy(FIXTURE["unsigned_handoff_570"])]
            random.Random(seed + 100).shuffle(events)
            self.assertEqual(payable_minutes(events), frozen_reference)
            self.assertEqual(frozen_minutes(events), 60)


class TwoLegalHandoffPathsTest(unittest.TestCase):
    def test_path_a_signed_handoff_at_570_pays_driver1_180_driver2_60(self):
        events = base_events() + [handoff_570()]
        pay = payable_minutes(events)
        self.assertEqual(pay, {"driver-1": 180, "driver-2": 60})
        self.assertEqual(frozen_minutes(events), 0)
        op_shifts = shifts(events)
        self.assertEqual([(r["start_minute"], r["end_minute"]) for r in op_shifts["driver-1"]],
                         [(480, 570), (630, 720)])
        self.assertEqual([(r["start_minute"], r["end_minute"]) for r in op_shifts["driver-2"]],
                         [(570, 600), (600, 630)])
        self.assertEqual([(r["start_minute"], r["end_minute"]) for r in op_shifts["driver-2"].merged],
                         [(570, 630)])

    def test_path_b_handoff_630_and_coaching_keeps_driver1_240(self):
        events = base_events() + [handoff_630(), coaching()]
        self.assertEqual(payable_minutes(events), {"driver-1": 240})
        self.assertEqual(coaching_minutes(events), {"driver-2": 60})
        self.assertEqual(frozen_minutes(events), 0)
        self.assertNotIn("driver-2", payable_minutes(events))
        d2_rows = shifts(events)["driver-2"]
        self.assertTrue(all(r["kind"] in ("coach", "ride") for r in d2_rows))
        self.assertEqual(sum(r["minutes"] for r in d2_rows if r["kind"] == "coach"), 60)

    def test_two_paths_different_correct_conclusions_same_cards(self):
        path_a = payable_minutes(base_events() + [handoff_570()])
        path_b = payable_minutes(base_events() + [handoff_630(), coaching()])
        self.assertEqual(path_a["driver-1"], 180)
        self.assertEqual(path_b["driver-1"], 240)
        self.assertEqual(path_a["driver-2"], 60)
        self.assertNotIn("driver-2", path_b)


class SameMachineNoDoublePayTest(unittest.TestCase):
    def test_no_minute_payable_to_two_operators(self):
        for extra in ([handoff_570()], [handoff_630(), coaching()], []):
            result = reconcile(base_events() + copy.deepcopy(extra))
            for machine in result["machines"].values():
                for segment in machine["segments"]:
                    if segment["status"] == "payable":
                        self.assertTrue(segment["operator_id"])
                        self.assertEqual(len([segment["operator_id"]]), 1)
            unique_payable = sum(
                s["minutes"] for m in result["machines"].values()
                for s in m["segments"] if s["status"] == "payable")
            unique_frozen = sum(
                s["minutes"] for m in result["machines"].values()
                for s in m["segments"] if s["status"] == "disputed")
            # tractor-7 claimed horizon 480-720 = 240 minutes; every minute
            # is either singly payable or uniquely frozen, never double paid.
            self.assertEqual(unique_payable + unique_frozen, 240)
            self.assertLessEqual(unique_payable, 240)

    def test_coach_and_ride_listed_separately_from_driving(self):
        events = [
            {"id": "d1", "kind": "work", "operator_id": "driver-1",
             "machine_id": "tractor-7", "start_minute": 600, "end_minute": 720},
            {"id": "coach-card", "kind": "work", "role": "coach",
             "operator_id": "driver-2", "machine_id": "tractor-7",
             "start_minute": 600, "end_minute": 660},
            {"id": "ride-note", "kind": "companion", "mode": "ride",
             "operator_id": "driver-3", "driver_id": "driver-1",
             "machine_id": "tractor-7", "start_minute": 660, "end_minute": 720,
             "signed_by": ["driver-1", "driver-3"]},
        ]
        self.assertEqual(payable_minutes(events), {"driver-1": 120})
        self.assertEqual(coaching_minutes(events), {"driver-2": 60})
        self.assertEqual(ride_minutes(events), {"driver-3": 60})

    def test_unsigned_companion_note_does_not_rebut_driving_claim(self):
        unsigned_coach = coaching()
        unsigned_coach["signed_by"] = []
        unsigned_coach["signed"] = False
        unsigned_coach["id"] = "CA-UNSIGNED"
        events = base_events() + [unsigned_coach]
        self.assertEqual(frozen_minutes(events), 60)
        self.assertEqual(payable_minutes(events), {"driver-1": 180})


class ReuploadAndReplacementTest(unittest.TestCase):
    def test_duplicate_cards_in_event_list_do_not_double_pay(self):
        events = base_events()
        doubled = events + [copy.deepcopy(FIXTURE["reupload_card"])]
        self.assertEqual(payable_minutes(doubled), payable_minutes(events))
        renamed = copy.deepcopy(FIXTURE["reupload_renamed_copy"]["card"])
        with_new_id = copy.deepcopy(renamed)
        with_new_id["id"] = "BRAND-NEW-UPLOAD-ID"
        tripled = doubled + [renamed, with_new_id]
        self.assertEqual(payable_minutes(tripled), {"driver-1": 180})
        self.assertEqual(frozen_minutes(tripled), 60)

    def test_fingerprint_ignores_filename_and_upload_id(self):
        card = FIXTURE["reupload_card"]
        renamed = copy.deepcopy(FIXTURE["reupload_renamed_copy"]["card"])
        self.assertEqual(card_fingerprint(card), card_fingerprint(renamed))

    def test_capture_book_reupload_rename_counts_card_once(self):
        book = OperatorCapture()
        first = book.ingest(copy.deepcopy(FIXTURE["reupload_card"]),
                            filename="OC-D2-570-630.json")
        self.assertEqual(first["inserted"], 1)
        again = book.ingest(copy.deepcopy(FIXTURE["reupload_card"]),
                            filename="OC-D2-570-630.json")
        self.assertTrue(again["idempotent"])
        renamed = book.ingest(
            copy.deepcopy(FIXTURE["reupload_renamed_copy"]),
            filename="renamed-driver2-copy.json")
        self.assertTrue(renamed["idempotent"])
        self.assertEqual(len(book.all_cards()), 1)
        self.assertEqual(len({u["fingerprint"] for u in book.uploads}), 1)
        self.assertEqual({u["filename"] for u in book.uploads},
                         {"OC-D2-570-630.json", "renamed-driver2-copy.json"})

    def test_replacement_chain_is_traceable_and_pays_latest_only(self):
        book = OperatorCapture()
        v1, v2 = copy.deepcopy(FIXTURE["replacement_chain"])
        book.ingest(v1, filename="rc9-v1.json")
        second = book.ingest(v2, filename="rc9-v2-corrected.json")
        self.assertEqual(second["status"], "replacement")
        book.ingest(copy.deepcopy(v2), filename="rc9-v2-again-renamed.json")
        chain = book.chain("RC-9")
        self.assertEqual([row["version"] for row in chain], [1, 2])
        self.assertEqual(chain[0]["filename"], "rc9-v1.json")
        self.assertEqual(chain[-1]["filename"], "rc9-v2-corrected.json")
        pay = payable_minutes(book.effective_events())
        self.assertEqual(pay, {"driver-3": 90})


class LateAdjustmentAndBatchReadbackTest(unittest.TestCase):
    def _ledger_with_paid_240_batch(self):
        book = WageLedger()
        book.ingest([
            {"id": "OC-D1-480-600", "kind": "work", "operator_id": "driver-1",
             "machine_id": "tractor-7", "start_minute": 480, "end_minute": 600},
            {"id": "OC-D1-600-720", "kind": "work", "operator_id": "driver-1",
             "machine_id": "tractor-7", "start_minute": 600, "end_minute": 720},
        ])
        book.ingest(copy.deepcopy(FIXTURE["clean_other_machine"]))
        batch = book.pay_batch("PAY-2026-0601")
        self.assertEqual(batch["payable_minutes"],
                         {"driver-1": 240, "driver-3": 60})
        return book

    def test_late_handoff_adjusts_only_tractor7_570_630(self):
        book = self._ledger_with_paid_240_batch()
        book.ingest([e for e in base_events() if e.get("operator_id") == "driver-2"
                     or e["kind"] == "terminal"])
        adj = book.apply_late_evidence(handoff_570(), batch_id="PAY-2026-0601")
        self.assertEqual(adj["deltas"], {"driver-1": -60, "driver-2": 60})
        self.assertEqual(adj["machine_id"], "tractor-7")
        self.assertEqual((adj["start_minute"], adj["end_minute"]), (570, 630))
        self.assertEqual(adj["minutes"], 60)
        self.assertEqual(adj["scope"],
                         [{"machine_id": "tractor-7",
                           "start_minute": 570, "end_minute": 630}])
        self.assertNotIn("driver-3", adj["deltas"])
        self.assertEqual(book.reconcile()["payable_minutes"],
                         {"driver-1": 180, "driver-2": 60, "driver-3": 60})

    def test_late_evidence_after_frozen_batch_only_adds_driver2(self):
        book = OperatorCapture(base_events())
        batch = book.pay_batch("PAY-FROZEN")
        self.assertEqual(batch["payable_minutes"], {"driver-1": 180})
        self.assertEqual(batch["frozen_minutes"], 60)
        adj = book.create_adjustment(handoff_570(), batch_id="PAY-FROZEN")
        self.assertEqual(adj["deltas"], {"driver-2": 60})
        self.assertEqual(adj["frozen_minutes_before"], 60)
        self.assertEqual(adj["frozen_minutes_after"], 0)
        self.assertEqual((adj["start_minute"], adj["end_minute"]), (570, 630))

    def test_paid_batch_is_immutable_and_reads_back_original(self):
        book = self._ledger_with_paid_240_batch()
        stored = book.get_batch("PAY-2026-0601")
        stored["payable_minutes"]["driver-1"] = 999
        readback = book.read_batch("PAY-2026-0601")
        self.assertEqual(readback["payable_minutes"]["driver-1"], 240)
        with self.assertRaises(BatchLockedError):
            book.pay_batch("PAY-2026-0601")
        # Late adjustment must not rewrite the issued batch either.
        book.ingest(copy.deepcopy(FIXTURE["events"][2]))
        book.apply_late_evidence(handoff_570(), batch_id="PAY-2026-0601")
        self.assertEqual(book.get_batch("PAY-2026-0601")["payable_minutes"],
                         {"driver-1": 240, "driver-3": 60})

    def test_old_batch_reads_back_after_save_and_reopen(self):
        book = self._ledger_with_paid_240_batch()
        book.ingest(copy.deepcopy(FIXTURE["events"][2]))
        book.apply_late_evidence(handoff_570(), batch_id="PAY-2026-0601")
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "operator-ledger.json"
            book.save(path)
            reopened = WageLedger.load(path)
            old = reopened.get_batch("PAY-2026-0601")
            self.assertEqual(old["status"], "paid")
            self.assertEqual(old["payable_minutes"],
                             {"driver-1": 240, "driver-3": 60})
            self.assertEqual(len(reopened.adjustment_orders), 1)
            self.assertEqual(reopened.adjustment_orders[0]["deltas"],
                             {"driver-1": -60, "driver-2": 60})
            self.assertEqual(reopened.reconcile()["payable_minutes"],
                             {"driver-1": 180, "driver-2": 60, "driver-3": 60})

    def test_replay_recomputes_identical_conclusion(self):
        book = OperatorCapture(base_events())
        book.ingest(handoff_570())
        book.pay_batch("PAY-1")
        check = book.verify_replay()
        self.assertTrue(check["ok"])


class FixtureShapeTest(unittest.TestCase):
    def test_fixture_envelope_accepted_directly(self):
        self.assertEqual(payable_minutes(FIXTURE), {"driver-1": 180})
        result = reconcile(FIXTURE)
        self.assertEqual(result["frozen_minutes"], 60)


if __name__ == "__main__":
    unittest.main()
