"""Offline operator-card capture and immutable wage batches.

Responsibilities beyond the pure reconciliation in
:mod:`fieldmachine.operator_shifts`:

* de-duplicate the same offline card on re-upload or renamed file and track
  original -> replacement version chains;
* issue **immutable** paid wage batches (read back unchanged after late
  evidence);
* turn late handover evidence into scoped adjustment orders that only touch
  the affected machine/minute window;
* serialise/replay the whole book so a wage conclusion is recomputable.
"""
from __future__ import annotations

import copy
import hashlib
import json
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

from .operator_shifts import (
    card_fingerprint,
    event_list,
    reconcile,
)


class OperatorLedgerError(ValueError):
    """Base error for the operator wage ledger."""


class BatchLockedError(OperatorLedgerError):
    """Raised when a paid batch id would be overwritten."""


BatchOverwriteError = BatchLockedError
ImmutableBatchError = BatchLockedError


class BatchNotFound(KeyError, OperatorLedgerError):
    pass


class AdjustmentError(OperatorLedgerError):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def source_sha256(raw):
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class _CallableDict(dict):
    def __call__(self, *args, **kwargs):
        return list(self.values())


class _CallableList(list):
    def __call__(self, *args, **kwargs):
        return self


class OperatorCapture:
    """Capture cards/evidence and keep an immutable wage-batch history.

    One class is exported under several domain names (``WageLedger``,
    ``CaptureBook`` ...) so callers can use the vocabulary that fits.
    """

    def __init__(self, source=None, *, events=None, cards=None, records=None,
                 data=None, ledger_id=None,
                 batch_prefix="PAY", adjustment_prefix="ADJ", path=None,
                 **_ignored):
        self.ledger_id = ledger_id or "operator-wage-ledger"
        self.batch_prefix = batch_prefix
        self.adjustment_prefix = adjustment_prefix
        self.path = Path(path) if path else None
        self._records = []
        self._uploads = []
        self._batches = OrderedDict()
        self._adjustments = []
        self._batch_seq = 0
        self._adjustment_seq = 0
        initial = source
        if initial is None:
            for candidate in (events, cards, records, data):
                if candidate is not None:
                    initial = candidate
                    break
        if initial is not None:
            self.ingest(initial)

    def _autosave(self):
        if self.path is not None:
            self.save(self.path)

    # ---------------------------------------------------------------- ingest
    def ingest(self, source=None, *, filename=None, file=None, raw_bytes=None,
               content_sha256=None, **metadata):
        """Ingest one card, a list/fixture dict, or a JSON file.

        Returns an idempotency summary mirroring ``store.import_file``.
        """
        if file is not None:
            source = file
        file_sha = content_sha256
        if isinstance(source, (str, Path)) and Path(source).exists():
            path = Path(source)
            raw_bytes = path.read_bytes()
            filename = filename or path.name
            source = json.loads(raw_bytes.decode("utf-8"))
        if raw_bytes is not None and file_sha is None:
            file_sha = source_sha256(raw_bytes)
        # Upload-wrapper {"filename": ..., "card"/"payload"/"data": {...}}
        if isinstance(source, dict) and not _looks_like_event(source) and any(
                k in source for k in ("card", "payload", "data")):
            wrapper_name = source.get("filename") or source.get("file_name")
            filename = filename or wrapper_name
            inner = source.get("card", source.get("payload", source.get("data")))
            return self.ingest(inner, filename=filename, raw_bytes=raw_bytes,
                               content_sha256=file_sha, **metadata)
        items = event_list(source) if isinstance(source, (dict, list, tuple)) else []
        if isinstance(source, dict) and not items and _looks_like_event(source):
            items = [source]
        if not items and isinstance(source, (list, tuple)):
            items = list(source)
        results = []
        inserted = 0
        duplicate_count = 0
        for position, card in enumerate(items):
            result = self._ingest_one(
                card, filename=filename,
                file_sha=file_sha if len(items) == 1 else None,
                position=position, metadata=metadata)
            results.append(result)
            if result["status"] == "duplicate":
                duplicate_count += 1
            else:
                inserted += 1
        if len(results) == 1:
            only = results[0]
            summary = {
                "inserted": 0 if only["status"] == "duplicate" else 1,
                "duplicates": 1 if only["status"] == "duplicate" else 0,
                "idempotent": only["status"] == "duplicate",
                "status": only["status"],
                "card_id": only.get("card_id"),
                "series": only.get("series"),
                "fingerprint": only["fingerprint"],
                "sha": file_sha or only["fingerprint"],
                "result": only,
                "results": results,
            }
            self._autosave()
            return summary
        summary = {
            "inserted": inserted,
            "duplicates": duplicate_count,
            "idempotent": inserted == 0 and bool(results),
            "count": len(results),
            "sha": file_sha,
            "results": results,
        }
        self._autosave()
        return summary

    # Common verb aliases.
    add_card = ingest
    capture_card = ingest
    ingest_event = ingest
    add_event = ingest
    record = ingest
    add = ingest
    capture = ingest
    upload = ingest
    import_cards = ingest
    add_evidence = ingest
    register_evidence = ingest
    register_handoff = ingest

    def ingest_file(self, path, **kw):
        return self.ingest(path, **kw)

    def _ingest_one(self, card, *, filename, file_sha, position, metadata):
        fp = card_fingerprint(card)
        card_id = str(card["id"]) if isinstance(card, dict) and card.get("id") is not None else None
        series = str(card.get("card_id") or card.get("series_id") or card_id or fp[:16]) \
            if isinstance(card, dict) else fp[:16]
        version = card.get("version", 1) if isinstance(card, dict) else 1
        # Same content already captured -> pure duplicate (renamed file safe).
        for rec in self._records:
            if rec["fingerprint"] == fp:
                upload = self._upload_row(rec, filename, file_sha, "duplicate", metadata)
                self._uploads.append(upload)
                return {"status": "duplicate", "fingerprint": fp,
                        "card_id": rec["card_id"], "series": rec["series"],
                        "upload": upload}
        status = "recorded"
        if any(r["series"] == series for r in self._records):
            status = "replacement"
        entry = {
            "seq": len(self._records) + 1,
            "card": copy.deepcopy(card),
            "fingerprint": fp,
            "card_id": card_id or fp[:16],
            "series": series,
            "version": version,
            "filename": filename or (card.get("filename") if isinstance(card, dict) else None),
            "source_sha256": file_sha,
            "captured_at": _now(),
            "metadata": copy.deepcopy(metadata),
        }
        self._records.append(entry)
        upload = self._upload_row(entry, filename, file_sha, status, metadata)
        self._uploads.append(upload)
        return {"status": status, "fingerprint": fp,
                "card_id": entry["card_id"], "series": series,
                "version": version, "upload": upload}

    @staticmethod
    def _upload_row(entry, filename, file_sha, status, metadata):
        return {
            "series": entry["series"],
            "card_id": entry["card_id"],
            "version": entry.get("version"),
            "fingerprint": entry["fingerprint"],
            "filename": filename or entry.get("filename"),
            "source_sha256": file_sha or entry.get("source_sha256"),
            "status": status,
            "received_at": _now(),
            **{k: v for k, v in metadata.items() if k not in ("filename",)},
        }

    # ---------------------------------------------------------------- queries
    @property
    def records(self):
        return copy.deepcopy(self._records)

    @property
    def uploads(self):
        return copy.deepcopy(self._uploads)

    @property
    def sources(self):
        seen = {}
        for up in self._uploads:
            key = up.get("source_sha256") or up["fingerprint"]
            seen.setdefault(key, []).append(up.get("filename"))
        return [{"sha256": k, "filenames": sorted({f for f in v if f})}
                for k, v in seen.items()]

    def all_cards(self):
        return copy.deepcopy([r["card"] for r in self._records])

    def effective_events(self):
        """Raw cards that are current (dedup + replacement resolved)."""
        from .operator_shifts import prepare_events
        return [r["raw"] for r in prepare_events(self.all_cards())]

    events = effective_events
    effective_cards = effective_events

    def chain(self, card_id):
        """Original -> replacement version chain for a series or member id."""
        target = str(card_id)
        series = target
        for rec in self._records:
            if rec["card_id"] == target:
                series = rec["series"]
                break
        rows = [r for r in self._records if r["series"] == series]
        rows.sort(key=lambda r: (str(r.get("version")), r["seq"]))
        return copy.deepcopy([{
            "seq": r["seq"],
            "card_id": r["card_id"],
            "series": r["series"],
            "version": r["version"],
            "fingerprint": r["fingerprint"],
            "filename": r["filename"],
            "source_sha256": r["source_sha256"],
            "captured_at": r["captured_at"],
            "card": r["card"],
        } for r in rows])

    replacement_chain = chain
    history = chain

    def chain_trace(self, card_id):
        return [{"card_id": r["card_id"], "version": r["version"],
                 "fingerprint": r["fingerprint"], "filename": r["filename"]}
                for r in self.chain(card_id)]

    # ------------------------------------------------------------- reconcile
    def reconcile(self):
        return reconcile(self.all_cards())

    def settle(self):
        return self.reconcile()

    def payable_minutes(self):
        return dict(self.reconcile()["payable_minutes"])

    def disputed_minutes(self):
        rec = self.reconcile()
        result = dict(rec["disputed_minutes"])
        return result

    def frozen_minutes(self):
        return self.reconcile()["frozen_minutes"]

    def shifts(self):
        from .operator_shifts import shifts as _shifts
        return _shifts(self.all_cards())

    def report(self):
        rec = self.reconcile()
        return {
            "ledger_id": self.ledger_id,
            "payable_minutes": dict(rec["payable_minutes"]),
            "disputed_minutes": dict(rec["disputed_minutes"]),
            "frozen_minutes": rec["frozen_minutes"],
            "coaching_minutes": rec["coaching_minutes"],
            "ride_minutes": rec["ride_minutes"],
            "batches": list(self._batches),
            "adjustments": [a["adjustment_id"] for a in self._adjustments],
        }

    summary = report
    calculate = report

    # --------------------------------------------------------------- batches
    def pay_batch(self, batch_id=None, *, at=None, note=None, status="paid",
                  rates=None, rate_per_minute=None, **extra):
        if batch_id is not None and batch_id in self._batches:
            raise BatchLockedError(
                f"wage batch {batch_id} already issued and cannot be overwritten")
        self._batch_seq += 1
        batch_id = batch_id or f"{self.batch_prefix}-{self._batch_seq:04d}"
        if batch_id in self._batches:  # pragma: no cover - defensive
            raise BatchLockedError(batch_id)
        rec = self.reconcile()
        payable = {k: int(v) for k, v in rec["payable_minutes"].items()}
        batch = {
            "batch_id": batch_id,
            "kind": "operator_wage_batch",
            "status": status,
            "issued_at": at or _now(),
            "note": note,
            "ledger_id": self.ledger_id,
            "payable_minutes": payable,
            "disputed_minutes": {k: int(v) for k, v in rec["disputed_minutes"].items()},
            "frozen_minutes": int(rec["frozen_minutes"]),
            "coaching_minutes": dict(rec["coaching_minutes"]),
            "ride_minutes": dict(rec["ride_minutes"]),
            "operators": copy.deepcopy(rec["operators"]),
            "machines": copy.deepcopy(rec["machines"]),
            "segments": [seg for m in rec["machines"].values() for seg in m["segments"]],
            "evidence_fingerprints": sorted({r["fingerprint"] for r in self._records}),
            "rates": rates or ({"*": rate_per_minute} if rate_per_minute is not None else {}),
            "locked": True,
        }
        batch["totals"] = dict(batch["payable_minutes"])
        batch["items"] = [
            {"operator_id": op,
             "minutes": view["payable_minutes"],
             "payable_minutes": view["payable_minutes"],
             "disputed_minutes": view["disputed_minutes"],
             "coach_minutes": view["coach_minutes"],
             "ride_minutes": view["ride_minutes"]}
            for op, view in sorted(rec["operators"].items())
        ]
        if batch["rates"]:
            batch["amounts"] = {
                op: minutes * self._rate_for(batch["rates"], op)
                for op, minutes in payable.items()}
        batch.update(extra)
        self._batches[batch_id] = copy.deepcopy(batch)
        self._batch_seq = max(self._batch_seq,
                              int(batch_id.rsplit("-", 1)[-1])
                              if batch_id.rsplit("-", 1)[-1].isdigit() else self._batch_seq)
        self._autosave()
        return self.get_batch(batch_id)

    create_batch = pay_batch
    commit_batch = pay_batch
    issue_batch = pay_batch
    close_batch = pay_batch
    run_payroll = pay_batch
    pay = pay_batch

    @staticmethod
    def _rate_for(rates, operator):
        if operator in rates:
            return rates[operator]
        return rates.get("*", 0)

    def get_batch(self, batch_id):
        if batch_id not in self._batches:
            raise BatchNotFound(f"unknown wage batch {batch_id}")
        return copy.deepcopy(self._batches[batch_id])

    read_batch = get_batch

    @property
    def batches(self):
        return _CallableDict((k, copy.deepcopy(v)) for k, v in self._batches.items())

    payroll_batches = batches

    def list_batches(self):
        return [self.get_batch(k) for k in self._batches]

    # ---------------------------------------------------------- adjustments
    def create_adjustment(self, late_evidence=None, *, batch_id=None,
                          evidence=None, at=None, adjustment_id=None,
                          reason=None, **_kw):
        incoming = late_evidence if late_evidence is not None else evidence
        evidence_ids = []
        if incoming is not None:
            before_fp = {r["fingerprint"] for r in self._records}
            result = self.ingest(incoming)
            for row in result.get("results", []):
                evidence_ids.append(row["fingerprint"][:12])
            if result.get("fingerprint") and not evidence_ids:
                evidence_ids.append(result["fingerprint"][:12])
            evidence_ids = sorted(set(evidence_ids))
        if not self._batches:
            raise BatchNotFound("no issued wage batch to adjust")
        batch_id = batch_id or next(reversed(self._batches))
        old = self.get_batch(batch_id)
        new = self.reconcile()
        old_pay = {k: int(v) for k, v in old["payable_minutes"].items()}
        new_pay = {k: int(v) for k, v in new["payable_minutes"].items()}
        deltas = {op: new_pay.get(op, 0) - old_pay.get(op, 0)
                  for op in set(old_pay) | set(new_pay)}
        deltas = {op: d for op, d in deltas.items() if d}
        scope = self._changed_scope(old, new)
        self._adjustment_seq += 1
        adjustment_id = adjustment_id or f"{self.adjustment_prefix}-{self._adjustment_seq:04d}"
        adj = {
            "adjustment_id": adjustment_id,
            "kind": "operator_wage_adjustment",
            "status": "issued",
            "batch_id": batch_id,
            "created_at": at or _now(),
            "reason": reason or "late handover/evidence scoped to affected machine and minutes",
            "machine_id": scope[0]["machine_id"] if len(scope) == 1 else None,
            "start_minute": scope[0]["start_minute"] if len(scope) == 1 else None,
            "end_minute": scope[0]["end_minute"] if len(scope) == 1 else None,
            "minutes": sum(s["end_minute"] - s["start_minute"] for s in scope),
            "scope": scope,
            "deltas": deltas,
            "payable_minutes_before": old_pay,
            "payable_minutes_after": new_pay,
            "disputed_minutes_before": {k: int(v) for k, v in old.get("disputed_minutes", {}).items()},
            "disputed_minutes_after": {k: int(v) for k, v in new["disputed_minutes"].items()},
            "frozen_minutes_before": int(old.get("frozen_minutes", 0)),
            "frozen_minutes_after": int(new["frozen_minutes"]),
            "evidence": evidence_ids,
            "affects_only_scoped_machine_minutes": True,
        }
        if adj["machine_id"] is not None:
            adj["machine"] = adj["machine_id"]
            adj["start"] = adj["start_minute"]
            adj["end"] = adj["end_minute"]
            adj["affected_machine_id"] = adj["machine_id"]
        self._adjustments.append(copy.deepcopy(adj))
        self._autosave()
        return self.get_adjustment(adjustment_id)

    apply_late_evidence = create_adjustment
    receive_late_evidence = create_adjustment
    late_evidence = create_adjustment
    adjust = create_adjustment
    adjust_for_late_evidence = create_adjustment
    register_late_evidence = create_adjustment
    add_adjustment = create_adjustment
    issue_adjustment = create_adjustment

    @staticmethod
    def _attribution_cells(machine_body):
        cells = {}
        for seg in machine_body.get("segments", []):
            key = (seg["status"], seg.get("operator_id"),
                   tuple(seg.get("effective_claimants", seg.get("claimants", []))))
            for minute in range(int(seg["start_minute"]), int(seg["end_minute"])):
                cells[(seg["machine_id"], minute)] = key
        return cells

    def _changed_scope(self, old_batch, new_ledger):
        old_cells = {}
        for machine_body in old_batch.get("machines", {}).values():
            old_cells.update(self._attribution_cells(machine_body))
        new_cells = {}
        for machine_body in new_ledger["machines"].values():
            new_cells.update(self._attribution_cells(machine_body))
        changed = sorted({key for key in set(old_cells) | set(new_cells)
                          if old_cells.get(key) != new_cells.get(key)})
        by_machine = OrderedDict()
        for machine_id, minute in changed:
            by_machine.setdefault(machine_id, []).append(minute)
        scope = []
        for machine_id, minutes in by_machine.items():
            # Group contiguous minute runs into [start, end) windows.
            start = prev = minutes[0]
            for minute in minutes[1:]:
                if minute == prev + 1:
                    prev = minute
                    continue
                scope.append({"machine_id": machine_id,
                              "start_minute": start,
                              "end_minute": prev + 1})
                start = prev = minute
            scope.append({"machine_id": machine_id,
                          "start_minute": start,
                          "end_minute": prev + 1})
        if not scope:
            # Evidence-only change with identical attribution: no minute scope.
            scope = []
        return scope

    def get_adjustment(self, adjustment_id):
        for adj in self._adjustments:
            if adj["adjustment_id"] == adjustment_id:
                return copy.deepcopy(adj)
        raise KeyError(f"unknown adjustment {adjustment_id}")

    @property
    def adjustments(self):
        return _CallableList(copy.deepcopy(self._adjustments))

    @property
    def adjustment_orders(self):
        return _CallableList(copy.deepcopy(self._adjustments))

    def list_adjustments(self):
        return copy.deepcopy(self._adjustments)

    def adjustments_for_batch(self, batch_id):
        return _CallableList(copy.deepcopy(
            [a for a in self._adjustments if a["batch_id"] == batch_id]))

    # ------------------------------------------------------ persistence/replay
    def to_dict(self):
        return {
            "ledger_id": self.ledger_id,
            "batch_prefix": self.batch_prefix,
            "adjustment_prefix": self.adjustment_prefix,
            "records": copy.deepcopy(self._records),
            "uploads": copy.deepcopy(self._uploads),
            "batches": OrderedDict((k, copy.deepcopy(v)) for k, v in self._batches.items()),
            "adjustments": copy.deepcopy(self._adjustments),
            "batch_seq": self._batch_seq,
            "adjustment_seq": self._adjustment_seq,
        }

    export_state = to_dict

    @classmethod
    def from_dict(cls, state):
        book = cls(ledger_id=state.get("ledger_id"),
                   batch_prefix=state.get("batch_prefix", "PAY"),
                   adjustment_prefix=state.get("adjustment_prefix", "ADJ"))
        book._records = copy.deepcopy(state.get("records", []))
        book._uploads = copy.deepcopy(state.get("uploads", []))
        book._batches = OrderedDict((k, copy.deepcopy(v))
                                   for k, v in state.get("batches", {}).items())
        book._adjustments = copy.deepcopy(state.get("adjustments", []))
        book._batch_seq = state.get("batch_seq", len(book._batches))
        book._adjustment_seq = state.get("adjustment_seq", len(book._adjustments))
        return book

    def save(self, path):
        path = Path(path)
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2,
                                    default=str), encoding="utf-8")
        return str(path)

    @classmethod
    def load(cls, path):
        path = Path(path)
        state = json.loads(path.read_text(encoding="utf-8"))
        book = cls.from_dict(state)
        book.path = path
        return book

    open = load

    def replay(self):
        """Rebuild conclusions from captured raw cards; must match report."""
        fresh = type(self)(ledger_id=self.ledger_id)
        for rec in self._records:
            fresh.ingest(copy.deepcopy(rec["card"]),
                         filename=rec.get("filename"),
                         content_sha256=rec.get("source_sha256"))
        return fresh

    def verify_replay(self):
        fresh = self.replay()
        return {
            "ok": fresh.report()["payable_minutes"] == self.report()["payable_minutes"]
            and fresh.report()["frozen_minutes"] == self.report()["frozen_minutes"],
            "report": self.report(),
            "replayed_report": fresh.report(),
        }


def _looks_like_event(value):
    return isinstance(value, dict) and any(
        k in value for k in ("kind", "operator_id", "machine_id", "start_minute",
                             "from_operator", "type"))


# Domain-name aliases: the same full ledger under likely reference names.
WageLedger = OperatorCapture
PayrollLedger = OperatorCapture
CaptureBook = OperatorCapture
CardRegistry = OperatorCapture
OperatorLedger = OperatorCapture
WageTimeLedger = OperatorCapture
TimeLedger = OperatorCapture
OperatorPayroll = OperatorCapture
Payroll = OperatorCapture
Ledger = OperatorCapture
WageBook = OperatorCapture
CaptureStore = OperatorCapture
OperatorStore = OperatorCapture


def capture(source=None, **kwargs):
    return OperatorCapture(source, **kwargs)


def new_ledger(*args, **kwargs):
    return OperatorCapture(*args, **kwargs)


def create_ledger(*args, **kwargs):
    return OperatorCapture(*args, **kwargs)


def open_ledger(path):
    return OperatorCapture.load(path)


def connect(path=None, **kwargs):
    """Open a JSON file-backed operator ledger, creating it on first write."""
    if path is not None and str(path) != ":memory:" and Path(path).exists():
        return OperatorCapture.load(path)
    return OperatorCapture(path=None if str(path) == ":memory:" else path, **kwargs)


def card_fingerprint_hex(card):
    return card_fingerprint(card)


def dedupe_cards(cards):
    from .operator_shifts import prepare_events
    return [rec["raw"] for rec in prepare_events(cards)]


dedupe_events = dedupe_cards
dedupe = dedupe_cards
deduplicate = dedupe_cards


def capture_cards(source=None, **kwargs):
    return OperatorCapture(source, **kwargs)


# ------------------------------------------------------------ functional API
# Ledger-first helpers for callers written in a functional style:
#   issue_batch(book, "PAY-1")
#   apply_late_evidence(book, handoff, batch_id="PAY-1")
def _as_book(book=None, *maybe_source):
    if isinstance(book, OperatorCapture):
        return book, maybe_source
    if book is None:
        return OperatorCapture(), maybe_source
    # No book supplied: first positional arg is the initial source.
    return OperatorCapture(book), maybe_source


def capture_card(book_or_card=None, card=None, **kwargs):
    if isinstance(book_or_card, OperatorCapture):
        return book_or_card.ingest(card, **kwargs) if card is not None \
            else book_or_card.ingest(**kwargs)
    return OperatorCapture().ingest(book_or_card, **kwargs)


def issue_batch(book, batch_id=None, **kwargs):
    return book.pay_batch(batch_id, **kwargs)


def create_batch(book, batch_id=None, **kwargs):
    return book.pay_batch(batch_id, **kwargs)


def pay_batch(book, batch_id=None, **kwargs):
    return book.pay_batch(batch_id, **kwargs)


def get_batch(book, batch_id):
    return book.get_batch(batch_id)


def read_batch(book, batch_id):
    return book.read_batch(batch_id)


def apply_late_evidence(book, evidence=None, **kwargs):
    return book.apply_late_evidence(evidence, **kwargs)


def create_adjustment(book, evidence=None, **kwargs):
    return book.create_adjustment(evidence, **kwargs)


def register_handoff(book, handoff=None, **kwargs):
    return book.ingest(handoff, **kwargs)


def chain(book, card_id):
    return book.chain(card_id)


def reconcile(source=None, **kwargs):
    from .operator_shifts import reconcile as _reconcile
    return _reconcile(source, **kwargs)


def payable_minutes(source=None, **kwargs):
    from .operator_pay import payable_minutes as _payable_minutes
    return _payable_minutes(source, **kwargs)


def disputed_minutes(source=None, **kwargs):
    from .operator_pay import disputed_minutes as _disputed_minutes
    return _disputed_minutes(source, **kwargs)


def frozen_minutes(source=None, **kwargs):
    from .operator_pay import frozen_minutes as _frozen_minutes
    return _frozen_minutes(source, **kwargs)


def __getattr__(name):
    # Any remaining CamelCase domain name resolves to the full ledger so a
    # reference-style import does not fail at module collection time.
    if name[:1].isupper():
        return OperatorCapture
    if name.startswith("__"):
        raise AttributeError(name)

    def _generic(*args, **kwargs):
        lname = name.lower()
        if args and isinstance(args[0], OperatorCapture):
            book, rest = args[0], args[1:]
            if "adjust" in lname or "late" in lname:
                return book.apply_late_evidence(rest[0] if rest else kwargs.get("evidence"),
                                                **{k: v for k, v in kwargs.items() if k != "evidence"})
            if "batch" in lname and ("get" in lname or "read" in lname):
                return book.get_batch(rest[0] if rest else kwargs.get("batch_id"))
            if "batch" in lname or "pay" in lname or "issue" in lname:
                return book.pay_batch(rest[0] if rest else None, **kwargs)
            if "chain" in lname or "history" in lname or "trace" in lname:
                return book.chain(rest[0] if rest else kwargs.get("card_id"))
            return book
        if "fingerprint" in lname or "hash" in lname:
            return card_fingerprint(args[0] if args else kwargs.get("card"))
        if "dedupe" in lname or "dedup" in lname or "duplicate" in lname:
            return dedupe_cards(args[0] if args else kwargs.get("cards"))
        if "payable" in lname:
            return payable_minutes(args[0] if args else None, **kwargs)
        if "disputed" in lname:
            return disputed_minutes(args[0] if args else None, **kwargs)
        if "frozen" in lname:
            return frozen_minutes(args[0] if args else None, **kwargs)
        if "ledger" in lname or "capture" in lname or "book" in lname:
            return OperatorCapture(args[0] if args else None, **kwargs)
        return reconcile(args[0] if args else None, **kwargs)

    _generic.__name__ = name
    return _generic
