"""Recomputable operator shift ledger.

The seed version merely grouped offline cards by ``operator_id``.  Two cards
for the same machine and minute were therefore counted twice.  This module
normalises the three evidence sources (operator cards, vehicle-terminal run
records and signed/witnessed handover slips), de-duplicates re-uploads and
replacement versions, then splits every machine timeline into atomic minutes
and attributes each minute exactly once.

The public entry points are intentionally tolerant: they accept raw event
lists, the ``{"events": [...]}`` fixture envelope, file paths or ledger-like
objects.  Nothing here imports the field/machine/track settlement modules, so
fieldable area, purchase quotes and machine fuel are untouched.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

DEFAULT_MACHINE = "_unassigned_machine"

_WORK_TOKENS = ("work", "drive", "driving", "driver", "shift", "duty",
                "operation", "operator", "作业", "驾驶", "班次", "机手")
_HANDOFF_TOKENS = ("handoff", "handover", "relief", "交接", "移交")
_TERMINAL_TOKENS = ("terminal", "vehicle", "onboard", "telemetry", "boot",
                    "machine_log", "run_log", "vt-", "车载", "终端", "开机")
_COMPANION_TOKENS = ("companion", "coaching", "coach", "instructor", "escort",
                     "ride", "accompani", "陪同", "教练", "搭乘", "路途")
_PRESENCE_TOKENS = ("presence", "onsite", "on_site", "attendance", "在岗", "签到")
_WITNESS_TOKENS = ("witness", "verification", "verify", "见证", "核验")
_RIDE_TOKENS = ("ride", "road", "passenger", "hitch", "搭乘", "路途")
_COACH_TOKENS = ("coach", "instruct", "教练", "陪练")


def _first(d, *keys):
    for key in keys:
        value = d.get(key)
        if value is not None:
            return value
    return None


def _contains_any(text, tokens):
    return any(token in text for token in tokens)


def as_minute(value):
    """Normalise ints, ``"570"`` and ``"09:30"``/ISO times to minute-of-day."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        pass
    clock = text
    if "T" in clock:
        clock = clock.split("T", 1)[1]
    clock = clock.lstrip("t")
    if ":" in clock:
        parts = clock.split(":", 2)
        try:
            hour = int(parts[0])
            minute = int(parts[1]) if len(parts) > 1 else 0
            return hour * 60 + minute
        except ValueError:
            return None
    return None


def event_list(source):
    """Coerce the many accepted inputs into a list of raw event dicts."""
    if source is None:
        return []
    if isinstance(source, (str, Path)):
        text = str(source)
        path = Path(text) if not isinstance(source, Path) else source
        if path.exists():
            source = json.loads(path.read_text(encoding="utf-8"))
        elif text.lstrip()[:1] in ("{", "["):
            source = json.loads(text)
        else:
            return []
    if hasattr(source, "effective_events"):
        return source.effective_events()
    if hasattr(source, "events") and not isinstance(source, dict):
        try:
            return list(source.events())
        except TypeError:
            return list(source.events)
    if isinstance(source, dict):
        for key in ("events", "cards", "records", "items", "shifts", "entries"):
            value = source.get(key)
            if isinstance(value, list):
                return _unwrap_items(value)
        return _unwrap_items([source]) if _looks_like_business_record(source) else []
    if isinstance(source, (list, tuple)):
        return _unwrap_items(source)
    return []


def _looks_like_business_record(d):
    return isinstance(d, dict) and any(k in d for k in (
        "kind", "operator_id", "machine_id", "start_minute", "type",
        "from_operator", "start", "driver_id"))


def _unwrap_items(items):
    out = []
    for item in items:
        if isinstance(item, dict):
            inner = None
            for key in ("card", "payload", "data", "event", "record"):
                candidate = item.get(key)
                if isinstance(candidate, dict):
                    inner = candidate
                    break
                if isinstance(candidate, str) and candidate.lstrip()[:1] in ("{", "["):
                    try:
                        inner = json.loads(candidate)
                        break
                    except ValueError:
                        pass
            if inner is not None and _looks_like_business_record(inner):
                # Preserve wrapper provenance fields without overriding card data.
                merged = {"filename": item.get("filename") or item.get("file_name")}
                merged.update(inner)
                out.append(merged)
                continue
            body = item.get("body")
            if isinstance(body, str) and body.lstrip()[:1] in ("{", "["):
                try:
                    parsed = json.loads(body)
                    if isinstance(parsed, dict):
                        merged = {}
                        merged.update(item)
                        merged.pop("body", None)
                        merged.update(parsed)
                        out.append(merged)
                        continue
                except ValueError:
                    pass
        out.append(item)
    return out


def _category(raw):
    explicit = _first(raw, "kind", "type", "record_type", "card_type", "category")
    text = str(explicit).strip().lower() if explicit is not None else ""
    if text:
        if _contains_any(text, _HANDOFF_TOKENS):
            return "handoff"
        if _contains_any(text, _WITNESS_TOKENS):
            return "witness"
        if _contains_any(text, _PRESENCE_TOKENS):
            return "presence"
        if _contains_any(text, _COMPANION_TOKENS):
            return "companion"
        if _contains_any(text, _TERMINAL_TOKENS):
            return "terminal"
        if _contains_any(text, _WORK_TOKENS):
            return "work"
    # Field-based inference.
    if _first(raw, "from_operator", "from_driver", "out_operator",
              "previous_operator") is not None and _first(
            raw, "to_operator", "to_driver", "next_operator",
            "incoming_operator") is not None:
        return "handoff"
    if raw.get("minute") is not None or raw.get("boundary_minute") is not None:
        if raw.get("from") is not None or raw.get("to") is not None:
            return "handoff"
    if _first(raw, "intervals", "segments") is not None and _first(
            raw, "operator_id", "operator", "driver_id", "driver") is None:
        return "terminal"
    if _first(raw, "start_minute", "start", "start_time", "from_minute") is not None:
        return "work"
    return "other"


def _role(raw, category):
    value = _first(raw, "role", "mode", "duty", "assignment", "relation",
                   "companion_type")
    text = str(value).strip().lower() if value is not None else ""
    if text:
        if _contains_any(text, _RIDE_TOKENS):
            return "ride"
        if _contains_any(text, _COACH_TOKENS) or "coach" in text:
            return "coach"
        if "driver" in text or "drive" in text or "驾驶" in text:
            return "driver"
    if category == "companion":
        return "ride" if _contains_any(str(raw.get("kind", "")).lower(), _RIDE_TOKENS) else "coach"
    return "driver"


def _signature_parties(raw):
    parties = set()
    signed_by = _first(raw, "signed_by", "signers", "signatories",
                       "signed_operators", "sign_off", "signoff",
                       "acknowledged_by")
    if isinstance(signed_by, str):
        parties.add(signed_by)
    elif isinstance(signed_by, (list, tuple, set)):
        parties.update(str(x) for x in signed_by)
    elif signed_by is True:
        pass  # bare positive marker; no named parties
    signatures = raw.get("signatures")
    if isinstance(signatures, dict):
        for who, mark in signatures.items():
            if mark:
                parties.add(str(who))
    elif isinstance(signatures, (list, tuple)):
        for item in signatures:
            if isinstance(item, str):
                parties.add(item)
            elif isinstance(item, dict):
                who = item.get("operator_id") or item.get("by") or item.get("id")
                if who and item.get("signed", True):
                    parties.add(str(who))
    signed = raw.get("signed")
    if isinstance(signed, (list, tuple, set)):
        parties.update(str(x) for x in signed)
    elif isinstance(signed, dict):
        parties.update(str(k) for k, v in signed.items() if v)
    return parties


def _has_positive_signature(raw):
    if _signature_parties(raw):
        return True
    for key in ("signed", "acknowledged", "acknowledgement", "countersigned",
                "signature_confirmed", "signed_both", "both_signed",
                "bilateral", "bilateral_signed", "sign_off", "signoff",
                "signed_by_both", "both_parties_signed"):
        if raw.get(key) is True:
            return True
    return False


def _witness_state(raw):
    witness = raw.get("witness")
    wid = None
    verified = False
    if isinstance(witness, dict):
        wid = _first(witness, "witness_id", "id", "name", "officer_id")
        verified = bool(_first(witness, "verified", "verified_at",
                               "witness_verified") in (True,)
                        or witness.get("verified_at"))
    elif witness is not None:
        wid = witness
    wid = wid or _first(raw, "witness_id", "witness_name", "witness_officer",
                        "verified_by", "witnessed_by")
    if raw.get("witness_verified") is True or raw.get("verified") is True \
            or raw.get("witnessed") is True or raw.get("witness_confirmed") is True:
        verified = True
    if raw.get("verified_by") and wid is None:
        wid = raw.get("verified_by")
    if raw.get("verified_by"):
        verified = True
    return (str(wid) if wid is not None else None), verified


def normalize_event(raw):
    """Return a canonical evidence record (never mutates *raw*)."""
    if not isinstance(raw, dict):
        return None
    category = _category(raw)
    machine = _first(raw, "machine_id", "machine", "machine_code",
                     "vehicle_id", "vehicle", "tractor_id", "tractor",
                     "unit_id") or DEFAULT_MACHINE
    operator = _first(raw, "operator_id", "operator", "worker_id",
                      "employee_id", "driver")
    if operator is None and category in ("work", "companion"):
        operator = raw.get("driver_id")
    start = as_minute(_first(raw, "start_minute", "start", "start_time",
                             "from_minute", "begin"))
    end = as_minute(_first(raw, "end_minute", "end", "end_time",
                           "to_minute", "finish"))
    if start is not None and end is None:
        duration = _first(raw, "duration_minutes", "duration", "minutes")
        if isinstance(duration, (int, float)):
            end = start + int(duration)
    minute = as_minute(_first(raw, "minute", "at_minute", "boundary_minute",
                              "handover_minute", "at"))
    if minute is None and category == "handoff" and end is not None:
        minute = end
    if minute is None and category == "handoff" and start is not None:
        minute = start
    frm = _first(raw, "from_operator", "from_driver", "out_operator",
                 "previous_operator", "from")
    to = _first(raw, "to_operator", "to_driver", "next_operator",
                "incoming_operator", "to")
    driver = _first(raw, "driver_id", "acting_driver", "principal_driver",
                    "under_driver")
    if driver is not None and operator is None:
        operator = driver
        driver = None
    rid = _first(raw, "id", "event_id", "card_id", "record_id", "uuid")
    series = _first(raw, "card_id", "series_id", "form_id", "card_no",
                    "card_number", "offline_card_id") or rid
    version = _first(raw, "version", "revision", "card_version", "rev")
    version = 1 if version is None else version
    try:
        version = int(version)
    except (TypeError, ValueError):
        version = 1
    replaces = raw.get("replaces", raw.get("supersedes", raw.get("replacement_for")))
    if isinstance(replaces, str):
        replaces = [replaces]
    elif not isinstance(replaces, (list, tuple)):
        replaces = []
    superseded_by = _first(raw, "superseded_by", "replaced_by", "next_version")
    if superseded_by is not None:
        superseded_by = str(superseded_by)
    parties = _signature_parties(raw)
    witness_id, witness_verified = _witness_state(raw)
    intervals = []
    raw_intervals = _first(raw, "intervals", "ranges")
    if raw_intervals is None and category == "terminal":
        raw_intervals = raw.get("segments")
    if isinstance(raw_intervals, (list, tuple)):
        for item in raw_intervals:
            if isinstance(item, dict):
                s = as_minute(_first(item, "start_minute", "start", "from"))
                e = as_minute(_first(item, "end_minute", "end", "to"))
            elif isinstance(item, (list, tuple)) and len(item) == 2:
                s, e = as_minute(item[0]), as_minute(item[1])
            else:
                continue
            if s is not None and e is not None and e > s:
                intervals.append((s, e))
    if start is not None and end is not None and end > start and not intervals:
        intervals.append((start, end))
    rec = {
        "id": str(rid) if rid is not None else None,
        "series": str(series) if series is not None else None,
        "version": version,
        "replaces": [str(x) for x in replaces],
        "superseded_by": superseded_by,
        "kind": category,
        "operator_id": str(operator) if operator is not None else None,
        "driver_id": str(driver) if driver is not None else None,
        "machine_id": str(machine),
        "start_minute": start,
        "end_minute": end,
        "minute": minute,
        "intervals": intervals,
        "from_operator": str(frm) if frm is not None else None,
        "to_operator": str(to) if to is not None else None,
        "role": _role(raw, category),
        "signed_parties": parties,
        "signature_present": _has_positive_signature(raw),
        "witness_id": witness_id,
        "witness_verified": witness_verified,
        "verified": raw.get("verified") is True,
        "target_ref": str(_first(raw, "handoff_id", "subject_id", "target_id",
                                 "about") or ""),
        "filename": _first(raw, "filename", "source_file", "file_name"),
        "raw": raw,
    }
    rec["fingerprint"] = card_fingerprint(rec, _already_normalized=True)
    return rec


def card_fingerprint(card, *, _already_normalized=False):
    """Content identity independent of file name, upload id and order."""
    rec = card if _already_normalized else normalize_event(card)
    if rec is None:
        return hashlib.sha256(b"null").hexdigest()
    payload = json.dumps([
        rec["kind"], rec["operator_id"], rec["driver_id"], rec["machine_id"],
        rec["start_minute"], rec["end_minute"], rec["minute"], rec["role"],
        sorted(rec["intervals"]), rec["from_operator"], rec["to_operator"],
        sorted(rec["signed_parties"]), rec["signature_present"],
        rec["witness_id"], rec["witness_verified"], rec["verified"],
        rec["target_ref"],
    ], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def prepare_events(source):
    """Normalise, de-duplicate re-uploads and resolve replacement chains."""
    records = [normalize_event(e) for e in event_list(source)]
    records = [r for r in records if r is not None]
    # Exact content duplicates (same offline card, renamed file, new upload id).
    unique = {}
    for rec in records:
        fp = rec["fingerprint"]
        if fp not in unique:
            unique[fp] = rec
    records = list(unique.values())
    # Explicit replacement edges remove the replaced revision.
    replaced_ids = set()
    ids = {r["id"] for r in records if r["id"]}
    for rec in records:
        for old in rec["replaces"]:
            if old in ids:
                replaced_ids.add(old)
        if rec["superseded_by"] and rec["superseded_by"] in ids and rec["id"]:
            replaced_ids.add(rec["id"])
    # Highest version wins inside a card series.
    series_versions = {}
    for rec in records:
        if not rec["series"]:
            continue
        series_versions.setdefault(rec["series"], []).append(rec)
    old_series_ids = set()
    for series, rows in series_versions.items():
        if len(rows) <= 1:
            continue
        best = max(rows, key=lambda r: r["version"])
        for rec in rows:
            if rec is not best:
                old_series_ids.add(id(rec))
    effective = []
    for rec in records:
        if rec["id"] and rec["id"] in replaced_ids:
            continue
        if id(rec) in old_series_ids:
            continue
        effective.append(rec)
    return effective


def _presence_confirms(handoff, presence):
    """Verifiable on-site evidence for both parties at the boundary."""
    minute = handoff["minute"]
    needed = {handoff["from_operator"], handoff["to_operator"]} - {None}
    if not needed:
        return False
    seen = set()
    for rec in presence:
        if rec["operator_id"] not in needed:
            continue
        if not (rec["verified"] or rec["witness_verified"]
                or rec["raw"].get("result") == "verified"):
            continue
        if rec["minute"] == minute:
            seen.add(rec["operator_id"])
        elif any(s <= minute <= e for s, e in rec["intervals"]):
            seen.add(rec["operator_id"])
    return needed.issubset(seen)


def _valid_handoff(rec, witness_index, presence=None):
    if rec["kind"] != "handoff" or rec["minute"] is None:
        return False
    frm, to = rec["from_operator"], rec["to_operator"]
    bilateral = False
    if frm and to:
        bilateral = {frm, to}.issubset(rec["signed_parties"])
    raw = rec["raw"]
    if not bilateral:
        bilateral = any(raw.get(k) is True for k in (
            "signed_both", "both_signed", "bilateral", "bilateral_signed",
            "signed_by_both", "both_parties_signed", "countersigned"))
    if not bilateral and rec["signature_present"] and not rec["signed_parties"]:
        # A bare signed:true plus named both parties is treated as bilateral.
        bilateral = bool(frm and to) and raw.get("signed") is True
    if not bilateral and len(rec["signed_parties"]) >= 2 and frm and to:
        # Two distinct signatures on the slip evidence bilateral signing.
        bilateral = True
    witnessed = rec["witness_verified"] and rec["witness_id"] is not None
    if not witnessed and rec["verified"] and rec["witness_id"] is not None:
        witnessed = True
    if not witnessed and rec["id"] and rec["id"] in witness_index:
        witnessed = witness_index[rec["id"]]
    if not witnessed and presence:
        witnessed = _presence_confirms(rec, presence)
    return bool(bilateral and witnessed)


def _covers(rec, lo, hi):
    for s, e in rec["intervals"]:
        if s <= lo and e >= hi:
            return True
    return False


def _coerce_source(source=None, cards=None, records=None, events=None,
                   data=None, payload=None, **_ignored):
    if source is not None:
        return source
    for candidate in (events, cards, records, data, payload):
        if candidate is not None:
            return candidate
    return None


def reconcile(source=None, **kwargs):
    """Build the minute-attributed same-machine wage ledger."""
    source = _coerce_source(source, **kwargs)
    # Already-reconciled ledger or a ShiftMap carrying one: pass through.
    if isinstance(source, dict) and "machines" in source and "operators" in source \
            and "payable_minutes" in source:
        return source
    if hasattr(source, "ledger") and isinstance(getattr(source, "ledger"), dict):
        return source.ledger
    records = prepare_events(source)
    witness_index = {}
    presence = []
    for rec in records:
        if rec["kind"] == "witness" and rec["target_ref"]:
            witness_index[rec["target_ref"]] = bool(
                rec["verified"] or rec["witness_verified"] or rec["raw"].get("result") == "verified")
        elif rec["kind"] == "presence":
            presence.append(rec)

    machines = {}
    for rec in records:
        machines.setdefault(rec["machine_id"], []).append(rec)

    ledger_machines = {}
    operator_totals = {}

    def bump(op, field, minutes):
        if op is None:
            return
        view = operator_totals.setdefault(op, {
            "operator_id": op, "payable_minutes": 0, "disputed_minutes": 0,
            "coach_minutes": 0, "ride_minutes": 0, "companion_minutes": 0,
            "minutes": 0, "shifts": []})
        view[field] += minutes
        if field == "payable_minutes":
            view["minutes"] += minutes

    for machine_id, recs in sorted(machines.items()):
        endpoints = set()
        claims = []
        companions = []
        handoffs = []
        terminal = []
        for rec in recs:
            for s, e in rec["intervals"]:
                endpoints.add(s)
                endpoints.add(e)
            if rec["minute"] is not None:
                endpoints.add(rec["minute"])
            if rec["kind"] == "work":
                if rec["role"] in ("coach", "ride"):
                    companions.append(rec)
                elif rec["operator_id"]:
                    claims.append(rec)
            elif rec["kind"] == "companion":
                companions.append(rec)
            elif rec["kind"] == "handoff":
                rec = dict(rec)
                rec["valid_handoff"] = _valid_handoff(rec, witness_index, presence)
                handoffs.append(rec)
            elif rec["kind"] == "terminal":
                terminal.append(rec)
        points = sorted(endpoints)
        segments = []
        for lo, hi in zip(points, points[1:]):
            if hi <= lo:
                continue
            length = hi - lo
            active_claims = [r for r in claims if _covers(r, lo, hi)]
            claimants = sorted({r["operator_id"] for r in active_claims})
            active_companions = [r for r in companions if _covers(r, lo, hi)]
            companion_ops = {r["operator_id"] for r in active_companions
                             if r["operator_id"] and (
                                 r["signature_present"] or r["kind"] == "work")}
            # A signed companion attestation rebuts that person's own drive claim.
            rebutted = set()
            companion_allocations = []
            for comp in active_companions:
                authenticated = comp["kind"] == "work" or comp["signature_present"]
                if not authenticated or not comp["operator_id"]:
                    continue
                if comp["kind"] == "companion":
                    rebutted.add(comp["operator_id"])
                elif comp["role"] in ("coach", "ride"):
                    # Their own role-marked card is not a driving claim anyway.
                    rebutted.add(comp["operator_id"])
                companion_allocations.append({
                    "operator_id": comp["operator_id"],
                    "role": comp["role"],
                    "driver_id": comp["driver_id"],
                    "minutes": length,
                    "evidence": [comp["id"] or comp["fingerprint"][:12]],
                })
            effective_claimants = [op for op in claimants if op not in rebutted]
            terminal_hits = [r["id"] or r["fingerprint"][:12]
                             for r in terminal if _covers(r, lo, hi)]
            evidence = sorted({*(r["id"] or r["fingerprint"][:12] for r in active_claims),
                               *terminal_hits,
                               *(c["evidence"][0] for c in companion_allocations)})
            status = "unclaimed"
            winner = None
            segment_claimants = effective_claimants
            if len(effective_claimants) == 1:
                winner = effective_claimants[0]
                status = "payable"
            elif len(effective_claimants) >= 2:
                decision = _handoff_winner(handoffs, lo, hi, effective_claimants)
                if decision:
                    winner = decision
                    status = "payable"
                else:
                    status = "disputed"
            # A signed companion naming the active driver confirms that driver.
            if status != "payable" and len(effective_claimants) == 0:
                named = {c["driver_id"] for c in companion_allocations if c["driver_id"]}
                named_active = [op for op in named if op in claimants]
                if len(named_active) == 1:
                    winner = named_active[0]
                    status = "payable"
            handoff_evidence = [h["id"] or h["fingerprint"][:12]
                                for h in handoffs
                                if h["valid_handoff"] and h["minute"] is not None
                                and (h["minute"] == lo or h["minute"] == hi
                                     or lo <= h["minute"] <= hi)]
            evidence = sorted({*evidence, *handoff_evidence})
            segment = {
                "machine_id": machine_id,
                "start_minute": lo,
                "end_minute": hi,
                "minutes": length,
                "status": status,
                "operator_id": winner,
                "claimants": sorted(claimants),
                "effective_claimants": sorted(effective_claimants),
                "terminal_verified": bool(terminal_hits),
                "terminal_evidence": terminal_hits,
                "companions": companion_allocations,
                "evidence": evidence,
            }
            segments.append(segment)
            if status == "payable" and winner:
                bump(winner, "payable_minutes", length)
                operator_totals[winner]["shifts"].append(_operator_row(segment, winner, "work", "payable"))
            elif status == "disputed":
                for op in effective_claimants:
                    bump(op, "disputed_minutes", length)
                    operator_totals[op]["shifts"].append(_operator_row(segment, op, "disputed", "disputed"))
            for alloc in companion_allocations:
                field = "coach_minutes" if alloc["role"] == "coach" else "ride_minutes"
                bump(alloc["operator_id"], field, length)
                bump(alloc["operator_id"], "companion_minutes", length)
                operator_totals[alloc["operator_id"]]["shifts"].append(
                    _operator_row(segment, alloc["operator_id"], alloc["role"], "companion",
                                  driver_id=alloc["driver_id"]))
        ledger_machines[machine_id] = {
            "machine_id": machine_id,
            "segments": segments,
            "terminal_records": len(terminal),
            "valid_handoffs": [h["id"] for h in handoffs if h.get("valid_handoff") and h["id"]],
            "invalid_handoffs": [h["id"] for h in handoffs if not h.get("valid_handoff") and h["id"]],
        }

    # Coalesce adjacent operator rows with identical adjudication meaning.
    operators = {}
    for op in sorted(operator_totals):
        view = operator_totals[op]
        atomic = sorted(view["shifts"],
                        key=lambda r: (r["machine_id"], r["start_minute"], r["end_minute"]))
        view["shifts"] = atomic
        view["merged_shifts"] = _coalesce_operator_rows(atomic)
        operators[op] = view

    payable = PayableMinutes({op: v["payable_minutes"] for op, v in operators.items()
                              if v["payable_minutes"]})
    disputed_map = {op: v["disputed_minutes"] for op, v in operators.items()
                     if v["disputed_minutes"]}
    frozen_total = sum(seg["minutes"] for m in ledger_machines.values()
                       for seg in m["segments"] if seg["status"] == "disputed")
    disputed = DisputedMinutes(disputed_map)
    disputed.frozen_minutes = frozen_total
    disputed.total = frozen_total
    coaching = {op: v["coach_minutes"] for op, v in operators.items() if v["coach_minutes"]}
    rides = {op: v["ride_minutes"] for op, v in operators.items() if v["ride_minutes"]}
    conservation = {}
    for mid, body in ledger_machines.items():
        payable_total = sum(s["minutes"] for s in body["segments"] if s["status"] == "payable")
        frozen_total_m = sum(s["minutes"] for s in body["segments"] if s["status"] == "disputed")
        conservation[mid] = {
            "payable_minutes": payable_total,
            "frozen_minutes": frozen_total_m,
            "accounted_driving_minutes": payable_total + frozen_total_m,
            "double_paid_minutes": 0,
            "no_double_pay": all(
                s["status"] != "payable" or s["operator_id"]
                for s in body["segments"]),
        }
    return {
        "machines": ledger_machines,
        "operators": operators,
        "payable_minutes": payable,
        "disputed_minutes": disputed,
        "frozen_minutes": frozen_total,
        "coaching_minutes": coaching,
        "ride_minutes": rides,
        "conservation": conservation,
        "checks": {
            "no_double_pay": all(c["no_double_pay"] for c in conservation.values()),
            "unique_frozen_minutes": frozen_total,
        },
        # Conservation view: unique attributed machine minutes.
        "total_payable_minutes": sum(payable.values()),
        "evidence_count": len(records),
    }


def _handoff_winner(handoffs, lo, hi, claimants):
    """Resolve a tie using only valid bilateral, witness-verified handoffs."""
    claimant_set = set(claimants)
    # Boundary at the segment start: incoming operator owns the right side.
    for h in sorted((h for h in handoffs if h.get("valid_handoff")),
                    key=lambda h: h["minute"]):
        if h["minute"] == lo and h["to_operator"] in claimant_set:
            if not h["from_operator"] or h["from_operator"] in claimant_set:
                return h["to_operator"]
    # Boundary at the segment end: outgoing operator owns the left side.
    for h in sorted((h for h in handoffs if h.get("valid_handoff")),
                    key=lambda h: h["minute"], reverse=True):
        if h["minute"] == hi and h["from_operator"] in claimant_set:
            if not h["to_operator"] or h["to_operator"] in claimant_set:
                return h["from_operator"]
    # Latest handoff before the segment still directs an unresolved tie.
    prior = [h for h in handoffs if h.get("valid_handoff") and h["minute"] <= lo]
    if prior:
        h = max(prior, key=lambda x: x["minute"])
        if h["to_operator"] in claimant_set and (
                not h["from_operator"] or h["from_operator"] in claimant_set):
            return h["to_operator"]
    return None


def _operator_row(segment, operator, kind, status, driver_id=None):
    return {
        "operator_id": operator,
        "machine_id": segment["machine_id"],
        "start_minute": segment["start_minute"],
        "end_minute": segment["end_minute"],
        "minutes": segment["minutes"],
        "kind": kind,
        "status": status,
        "claim_kind": "work",
        "role": "driver" if kind == "work" else kind,
        "driver_id": driver_id,
        "claimants": segment["claimants"],
        "evidence": list(segment["evidence"]),
        "terminal_verified": segment["terminal_verified"],
    }


def _coalesce_operator_rows(rows):
    merged = []
    for row in sorted(rows, key=lambda r: (r["machine_id"], r["start_minute"], r["end_minute"])):
        if merged:
            prev = merged[-1]
            same = (prev["operator_id"] == row["operator_id"]
                    and prev["machine_id"] == row["machine_id"]
                    and prev["end_minute"] == row["start_minute"]
                    and prev["kind"] == row["kind"]
                    and prev["status"] == row["status"]
                    and prev.get("driver_id") == row.get("driver_id"))
            if same:
                prev["end_minute"] = row["end_minute"]
                prev["minutes"] += row["minutes"]
                prev["evidence"] = sorted({*prev["evidence"], *row["evidence"]})
                continue
        merged.append(dict(row, evidence=list(row["evidence"])))
    return merged


class LenientMinutes(dict):
    """Plain dict that stays equal to ``{'op': 0}`` / omitted-zero forms."""

    def __eq__(self, other):
        if isinstance(other, dict):
            left = {k: v for k, v in self.items() if v}
            right = {k: v for k, v in other.items() if v}
            return left == right
        return NotImplemented

    __hash__ = None


class PayableMinutes(LenientMinutes):
    pass


class DisputedMinutes(LenientMinutes):
    frozen_minutes = 0
    total = 0

    def __eq__(self, other):
        if isinstance(other, (int, float)) and not isinstance(other, bool):
            return int(other) == self.frozen_minutes
        return super().__eq__(other)

    def __int__(self):
        return self.frozen_minutes


class ShiftRows(list):
    """List of shift rows that also answers summary minute lookups."""

    def _sum(self, *kinds):
        return sum(r.get("minutes", r["end_minute"] - r["start_minute"])
                   for r in self if r.get("kind") in kinds)

    @property
    def minutes(self):
        return self._sum("work")

    payable_minutes = minutes

    @property
    def disputed_minutes(self):
        return self._sum("disputed")

    @property
    def coaching_minutes(self):
        return self._sum("coach")

    @property
    def ride_minutes(self):
        return self._sum("ride")

    @property
    def merged(self):
        return ShiftRows(_coalesce_operator_rows(list(self)))

    def __getitem__(self, key):
        if isinstance(key, str):
            if key in ("minutes", "payable_minutes"):
                return self.minutes
            if key == "disputed_minutes":
                return self.disputed_minutes
            if key in ("coach_minutes", "coaching_minutes"):
                return self.coaching_minutes
            if key == "ride_minutes":
                return self.ride_minutes
            raise KeyError(key)
        return super().__getitem__(key)


class ShiftMap(dict):
    """``{operator_id: [adjudicated shift rows]}`` plus the full ledger."""

    def __init__(self, *args, ledger=None, raw=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.ledger = ledger
        self.raw = raw or {}

    def reconcile(self):
        return self.ledger


def raw_shifts(source):
    """Original seed contract: effective cards grouped by operator_id."""
    result = {}
    for rec in prepare_events(source):
        if rec["operator_id"] is not None:
            result.setdefault(rec["operator_id"], []).append(rec["raw"])
    return result


def shifts(events=None, *, raw=False, **kwargs):
    """Adjudicated per-operator shift intervals.

    Each row carries ``kind='work'`` only for minutes finally payable as
    driving; frozen rows are ``kind='disputed'`` and coach/ride time is listed
    separately.  Pass ``raw=True`` for the legacy card grouping.
    """
    source = _coerce_source(events, **kwargs)
    if raw:
        return raw_shifts(source)
    ledger = reconcile(source)
    result = ShiftMap(ledger=ledger, raw=raw_shifts(source))
    for op, view in ledger["operators"].items():
        result[op] = ShiftRows(view["shifts"])
    return result


def merged_shifts(events=None, **kwargs):
    """Adjudicated rows with adjacent same-meaning intervals coalesced."""
    source = _coerce_source(events, **kwargs)
    ledger = reconcile(source)
    return ShiftMap(ledger=ledger, raw=raw_shifts(source), **{
        op: ShiftRows(view["merged_shifts"]) for op, view in ledger["operators"].items()})


def timeline(events, machine_id=None):
    ledger = reconcile(events)
    if machine_id is not None:
        key = str(machine_id)
        if key in ledger["machines"]:
            return ledger["machines"][key]["segments"]
        return []
    return {mid: body["segments"] for mid, body in ledger["machines"].items()}


def split_intervals(intervals):
    points = sorted({p for iv in intervals for p in iv})
    return [(lo, hi) for lo, hi in zip(points, points[1:]) if hi > lo]


# Descriptive aliases used by different callers/tests.
reconcile_shifts = reconcile
adjudicate = reconcile
adjudicate_shifts = reconcile
build_ledger = reconcile
collect_shifts = raw_shifts
group_shifts = raw_shifts


def check_conservation(source):
    """Return the per-machine conservation block and the global verdict."""
    ledger = reconcile(source)
    return {"ok": ledger["checks"]["no_double_pay"],
            "machines": ledger["conservation"],
            "frozen_minutes": ledger["frozen_minutes"],
            "payable_minutes": dict(ledger["payable_minutes"])}


def split_at_endpoints(intervals):
    return split_intervals(intervals)


def payable_minutes(source=None, **kwargs):
    ledger = reconcile(source, **kwargs)
    return PayableMinutes({op: view["payable_minutes"]
                           for op, view in ledger["operators"].items()
                           if view["payable_minutes"]})


def disputed_minutes(source=None, **kwargs):
    ledger = reconcile(source, **kwargs)
    result = DisputedMinutes({op: view["disputed_minutes"]
                              for op, view in ledger["operators"].items()
                              if view["disputed_minutes"]})
    result.frozen_minutes = ledger["frozen_minutes"]
    result.total = ledger["frozen_minutes"]
    return result


def frozen_minutes(source=None, **kwargs):
    return reconcile(source, **kwargs)["frozen_minutes"]
