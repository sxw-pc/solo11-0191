"""Offline firmware upgrade/recovery proof for a two-controller machine.

The machine has exactly two safety-relevant controllers:

* ``ECU1`` -- travel/drive controller (行驶控制器)
* ``ECU2`` -- work/implement controller (作业控制器)

A power loss can leave them on different versions.  This module proves,
entirely offline:

1. firmware byte signature + board adaptation + calibration schema all match;
2. controller combinations are certified by a vendor-signed matrix and any
   unconfirmed mixed combination grounds the whole machine;
3. active/pending dual-bank partitions are read back from modelled hardware;
4. the K1 -> K2 calibration conversion is real (not a version-label bump),
   has an atomic commit boundary and never performs its irreversible page
   program twice on retry;
5. downgrade needs a vendor recovery token bound to this machine;
6. two legal recoveries from one fixed breakpoint yield different safe
   function sets / dispatch capability;
7. old announcements/conclusions stay in an append-only history and the
   difference against new roadworthy conclusions is explainable.

The vendor signature scheme is an offline HMAC-SHA256 simulation rooted at
a well-known test key; it is tamper evident, not a production PKI.
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
from pathlib import Path

from .firmware import versions as report_versions

# ---------------------------------------------------------------------------
# Constants / scope
# ---------------------------------------------------------------------------

VENDOR_ID = 'fieldmachine-offline-vendor'
# Well-known offline proof key.  Production would use an asymmetric vendor root.
VENDOR_KEY = b'fieldmachine-offline-proof-root-2026-do-not-trust-in-prod'

ECU1 = 'ECU1'
ECU2 = 'ECU2'
CONTROLLERS = (ECU1, ECU2)
CONTROLLER_ROLES = {
    ECU1: 'travel_controller',      # 行驶控制器
    ECU2: 'work_controller',        # 作业控制器
}
BOARD_TYPES = {
    ECU1: 'DRIVE-CTRL-A1',
    ECU2: 'WORK-CTRL-B2',
}

SCHEMA_VERSION = 'fieldmachine-firmware-mixed/1'
DEFAULT_MACHINE_ID = 'FM-07'
FIXED_NOW = '2026-11-24T12:00:00'

# The result of this subsystem is safe functions + dispatch only.
SCOPE = {
    'decides': [
        'firmware_signature_and_board_fit',
        'calibration_schema_and_commit',
        'dual_bank_boot_verification',
        'cross_controller_compatibility',
        'safe_function_set',
        'dispatch_readiness',
    ],
    'excluded': [
        'plot_settlement',          # 地块结算
        'danger_evacuation_order',  # 险情撤离指令
        'allocation_route',         # 调拨路线
    ],
}

# Function catalogue.  Only the two functions that distinguish the routes are
# modelled; matrix entries decide which are certified for a combination.
FUNCTION_CATALOG = {
    'basic_tillage': {
        'zh': '基础耕作',
        'introduced': 'v1',
        'task_types': ['basic_tillage'],
    },
    'amplitude_control': {
        'zh': '变幅控制（幅宽/摆幅调节）',
        'introduced': 'v2',
        'aliases': ['variable_width_control', 'boom_amplitude_control'],
        'task_types': ['amplitude_tillage', 'variable_width_work'],
    },
}
# Key alias so callers using the descriptive name find the same function.
FUNCTION_CATALOG['variable_width_control'] = FUNCTION_CATALOG['amplitude_control']

BASE_FUNCTIONS = ['basic_tillage']
V2_FUNCTIONS = ['basic_tillage', 'amplitude_control']

CALIBRATION_SCHEMAS = ('K1', 'K2')
Q16 = 65536


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class FirmwareCaptureError(ValueError):
    """Base class; ``code`` is a stable machine-readable reason."""

    code = 'firmware_capture_error'

    def __init__(self, message=None, *, code=None, **details):
        super().__init__(message or (code or self.code))
        if code:
            self.code = code
        self.details = details


class SignatureInvalid(FirmwareCaptureError):
    code = 'signature_invalid'


class PackageVerificationError(FirmwareCaptureError):
    code = 'package_verification_failed'


class CalibrationError(FirmwareCaptureError):
    code = 'calibration_invalid'


class RecoveryTokenError(FirmwareCaptureError):
    code = 'recovery_token_invalid'


class RecoveryHalted(FirmwareCaptureError):
    """Internal control-flow signal: a recovery step failed safe."""

    def __init__(self, code, *, stage=None, reasons=None, **details):
        super().__init__(code, code=code)
        self.stage = stage
        self.reasons = reasons or [code]
        self.details = details


# ---------------------------------------------------------------------------
# Canonical JSON + vendor document signatures
# ---------------------------------------------------------------------------

def canonical_bytes(doc):
    return json.dumps(
        doc, sort_keys=True, separators=(',', ':'), ensure_ascii=False
    ).encode('utf-8')


def sha256_hex(blob):
    if isinstance(blob, str):
        blob = blob.encode('utf-8')
    return hashlib.sha256(blob).hexdigest()


def document_digest(doc):
    return sha256_hex(canonical_bytes(doc))


def sign_message(message, key=VENDOR_KEY):
    if isinstance(message, str):
        message = message.encode('utf-8')
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def seal_document(doc, key=VENDOR_KEY, *, signer=VENDOR_ID, field='signature'):
    """Return a copy of ``doc`` with a vendor signature over its body."""
    out = copy.deepcopy(doc)
    out.pop('signature', None)
    out.pop('vendor_signature', None)
    out['signer'] = signer
    out[field] = sign_message(canonical_bytes(out), key)
    return out


# Friendly alias.
vendor_sign = seal_document


def verify_seal(doc, key=VENDOR_KEY):
    """Verify a document produced by :func:`seal_document`."""
    if not isinstance(doc, dict):
        return False
    sig = doc.get('signature') or doc.get('vendor_signature')
    if not sig or not isinstance(sig, str):
        return False
    body = {k: v for k, v in doc.items() if k not in ('signature', 'vendor_signature')}
    expected = sign_message(canonical_bytes(body), key)
    return hmac.compare_digest(sig, expected)


verify_signature = verify_seal
check_signature = verify_seal


# ---------------------------------------------------------------------------
# Firmware packages: byte signature + board adaptation + calibration schema
# ---------------------------------------------------------------------------

def image_bytes(package):
    if package.get('image_b64') is not None:
        import base64
        return base64.b64decode(package['image_b64'])
    image = package.get('image', '')
    if isinstance(image, (bytes, bytearray)):
        return bytes(image)
    return str(image).encode('utf-8')


def _package_message(p):
    return '|'.join([
        'firmware-package-v1',
        str(p.get('package_id', '')),
        str(p.get('controller_id', '')),
        str(p.get('board_type', '')),
        str(p.get('version', '')),
        str(p.get('sha256', '')),
        str(p.get('calibration_schema', '')),
    ]).encode('utf-8')


def build_package(*, controller_id, version, board_type=None, image=None,
                  calibration_schema=None, package_id=None, key=VENDOR_KEY):
    """Create a vendor-signed firmware package."""
    board_type = board_type or BOARD_TYPES[controller_id]
    calibration_schema = calibration_schema or ('K2' if version == 'v2' else 'K1')
    package_id = package_id or f'FW-{controller_id}-{version}'
    if image is None:
        image = (
            f'FIELDDEVICE-OFFLINE-FIRMWARE\n'
            f'controller={controller_id}\nboard={board_type}\n'
            f'version={version}\nschema={calibration_schema}\n'
            f'build=vendor-signed-simulator\n'
        )
    digest = sha256_hex(image)
    pkg = {
        'package_id': package_id,
        'controller_id': controller_id,
        'board_type': board_type,
        'version': version,
        'calibration_schema': calibration_schema,
        'image': image if isinstance(image, str) else None,
        'sha256': digest,
        'signer': VENDOR_ID,
    }
    if not isinstance(image, str):
        import base64
        pkg['image'] = None
        pkg['image_b64'] = base64.b64encode(image).decode('ascii')
    pkg['signature'] = sign_message(_package_message(pkg), key)
    return pkg


def verify_package(package, *, board_type=None, calibration_schema=None,
                   key=VENDOR_KEY):
    """Verify the triple (byte signature, board fit, calibration schema).

    Returns a plain report dict; never raises for an invalid package.
    """
    errors = []
    checks = {
        'byte_digest': False,
        'byte_signature': False,
        'board_type': False,
        'calibration_schema_supported': False,
        'calibration_schema_match': None,
    }
    if not isinstance(package, dict):
        return {'ok': False, 'errors': ['package_not_object'], 'checks': checks}

    # 1) byte digest + vendor byte signature
    try:
        raw = image_bytes(package)
        actual_digest = sha256_hex(raw)
        checks['byte_digest'] = hmac.compare_digest(
            actual_digest, str(package.get('sha256', ''))
        )
    except Exception:  # malformed base64 etc.
        actual_digest = None
    if not checks['byte_digest']:
        errors.append('firmware_byte_digest_mismatch')

    expected_sig = sign_message(_package_message(package), key)
    sig = package.get('signature')
    checks['byte_signature'] = bool(
        isinstance(sig, str) and hmac.compare_digest(sig, expected_sig)
    )
    if not checks['byte_signature']:
        errors.append('firmware_signature_invalid')

    # 2) board adaptation
    pkg_board = package.get('board_type')
    if not isinstance(pkg_board, str) or not pkg_board:
        errors.append('board_type_missing')
        checks['board_type'] = False
    elif board_type is not None and pkg_board != board_type:
        errors.append(
            f'board_type_mismatch:package={pkg_board},hardware={board_type}'
        )
        checks['board_type'] = False
    else:
        checks['board_type'] = True

    # 3) calibration data schema
    schema = package.get('calibration_schema')
    checks['calibration_schema_supported'] = schema in CALIBRATION_SCHEMAS
    if not checks['calibration_schema_supported']:
        errors.append('calibration_schema_unsupported')
    if calibration_schema is not None:
        checks['calibration_schema_match'] = (schema == calibration_schema)
        if schema != calibration_schema:
            errors.append(
                f'calibration_schema_mismatch:package={schema},'
                f'required={calibration_schema}'
            )

    ok = (
        checks['byte_digest']
        and checks['byte_signature']
        and checks['board_type']
        and checks['calibration_schema_supported']
        and (calibration_schema is None or checks['calibration_schema_match'])
    )
    return {
        'ok': ok,
        'errors': errors,
        'checks': checks,
        'package_id': package.get('package_id'),
        'controller_id': package.get('controller_id'),
        'version': package.get('version'),
        'board_type': pkg_board,
        'calibration_schema': schema,
        'computed_sha256': actual_digest,
    }


verify_firmware = verify_package
check_firmware = verify_package
verify_image = verify_package
verify_firmware_package = verify_package


def assert_package(package, **kwargs):
    """Raise :class:`PackageVerificationError` if the triple does not match."""
    report = verify_package(package, **kwargs)
    if not report['ok']:
        raise PackageVerificationError(
            'firmware package verification failed',
            code='package_verification_failed',
            errors=report['errors'], report=report,
        )
    return report


# ---------------------------------------------------------------------------
# Calibration K1 -> K2: real conversion, strict validation, commit boundary
# ---------------------------------------------------------------------------

def default_k1(machine_id=DEFAULT_MACHINE_ID):
    return {
        'schema': 'K1',
        'machine_id': machine_id,
        'work_width_m': 3.0,
        'section_count': 3,
        'pulses_per_m': 58,
        'gain': 1.02,
        'offset_m': 0.015,
    }


def _is_number(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def validate_calibration(doc, schema=None):
    """Strictly validate a K1/K2 calibration document or page wrapper.

    A K1 document with only its ``schema`` label changed to ``K2`` fails:
    K2 requires fixed-point geometry, section widths and conversion digests.
    """
    errors = []
    if not isinstance(doc, dict):
        return {'ok': False, 'schema': None, 'errors': ['calibration_not_object']}

    # Accept page wrapper {"data": {...}, "digest": ...}.
    page_digest = None
    if 'data' in doc and isinstance(doc['data'], dict) and (
        'schema' not in doc or doc.get('data', {}).get('schema')
    ):
        page_digest = doc.get('digest')
        doc = doc['data']

    actual = doc.get('schema')
    if schema is not None and actual != schema:
        errors.append(f'schema_label_mismatch:doc={actual},required={schema}')
    if actual not in CALIBRATION_SCHEMAS:
        errors.append('schema_unsupported')
        return {'ok': False, 'schema': actual, 'errors': errors}

    if doc.get('machine_id') in (None, ''):
        errors.append('machine_id_missing')
    width = doc.get('work_width_m')
    if not _is_number(width) or not (0.1 < float(width) <= 24):
        errors.append('work_width_m_invalid')
    n = doc.get('section_count')
    if not isinstance(n, int) or isinstance(n, bool) or not (1 <= n <= 24):
        errors.append('section_count_invalid')
    pulses = doc.get('pulses_per_m')
    if not isinstance(pulses, int) or isinstance(pulses, bool) or pulses <= 0:
        errors.append('pulses_per_m_invalid')

    if actual == 'K1':
        gain = doc.get('gain')
        if not _is_number(gain) or not (0.5 <= float(gain) <= 2.0):
            errors.append('gain_invalid')
        offset = doc.get('offset_m')
        if not _is_number(offset) or abs(float(offset)) > 1.0:
            errors.append('offset_m_invalid')

    if actual == 'K2':
        for key in ('gain_q16', 'offset_mm', 'section_widths_m',
                    'amplitude_limit_deg', 'converted_from',
                    'source_digest', 'conversion_digest'):
            if key not in doc:
                errors.append(f'k2_field_missing:{key}')
        gq = doc.get('gain_q16')
        if not isinstance(gq, int) or not (Q16 // 2 <= gq <= 2 * Q16):
            errors.append('gain_q16_invalid')
        off = doc.get('offset_mm')
        if not isinstance(off, int) or abs(off) > 1000:
            errors.append('offset_mm_invalid')
        widths = doc.get('section_widths_m')
        if not isinstance(widths, list) or not widths or not all(
            _is_number(w) and float(w) > 0 for w in widths
        ):
            errors.append('section_widths_m_invalid')
        elif isinstance(n, int) and not isinstance(n, bool):
            if len(widths) != n:
                errors.append('section_widths_count_mismatch')
            elif _is_number(width):
                if abs(sum(float(w) for w in widths) - float(width)) > 1e-9:
                    errors.append('section_widths_sum_mismatch')
        amp = doc.get('amplitude_limit_deg')
        if not _is_number(amp) or not (0 < float(amp) <= 90):
            errors.append('amplitude_limit_deg_invalid')
        if doc.get('converted_from') != 'K1':
            errors.append('converted_from_not_K1')
        cd = doc.get('conversion_digest')
        if not isinstance(cd, str):
            errors.append('conversion_digest_missing')
        else:
            body = {k: v for k, v in doc.items() if k != 'conversion_digest'}
            if not hmac.compare_digest(cd, document_digest(body)):
                errors.append('conversion_digest_mismatch')

    if page_digest is not None:
        if not hmac.compare_digest(str(page_digest), document_digest(doc)):
            errors.append('page_digest_mismatch')

    return {'ok': not errors, 'schema': actual, 'errors': errors}


validate_cal = validate_calibration


def convert_calibration(k1):
    """Convert a valid K1 calibration into a complete, verifiable K2 document."""
    report = validate_calibration(k1, schema='K1')
    if not report['ok']:
        raise CalibrationError(
            'refusing to convert invalid K1 calibration',
            code='calibration_k1_invalid', errors=report['errors'],
        )
    width_mm_total = int(round(float(k1['work_width_m']) * 1000))
    n = int(k1['section_count'])
    base_mm, remainder = divmod(width_mm_total, n)
    widths_m = [
        (base_mm + (1 if i < remainder else 0)) / 1000.0
        for i in range(n)
    ]
    k2 = {
        'schema': 'K2',
        'machine_id': k1['machine_id'],
        'work_width_m': float(k1['work_width_m']),
        'section_count': n,
        'pulses_per_m': int(k1['pulses_per_m']),
        'gain_q16': int(round(float(k1['gain']) * Q16)),
        'offset_mm': int(round(float(k1['offset_m']) * 1000)),
        'section_widths_m': widths_m,
        # Deterministic vendor conversion rule for the v2 amplitude envelope.
        'amplitude_limit_deg': float(min(60, 20 + 5 * n)),
        'converted_from': 'K1',
        'source_digest': document_digest(k1),
    }
    k2['conversion_digest'] = document_digest(
        {k: v for k, v in k2.items() if k != 'conversion_digest'}
    )
    return k2


migrate_calibration = convert_calibration


def verify_conversion(k1, k2):
    """Re-run the conversion and compare, proving more than a label change."""
    errors = []
    v1 = validate_calibration(k1, schema='K1')
    if not v1['ok']:
        errors.extend(v1['errors'])
    v2 = validate_calibration(k2, schema='K2')
    if not v2['ok']:
        errors.extend(v2['errors'])
    if v1['ok'] and v2['ok']:
        if not hmac.compare_digest(k2.get('source_digest', ''),
                                   document_digest(k1)):
            errors.append('source_digest_mismatch')
        expected = convert_calibration(k1)
        if canonical_bytes(expected) != canonical_bytes(k2):
            errors.append('conversion_result_differs')
    return {'ok': not errors, 'errors': errors}


def _page_digest(page):
    data = page.get('data') if isinstance(page, dict) else None
    if not isinstance(data, dict):
        return None
    return document_digest(data)


def calibration_status(state):
    """Read calibration commit state from modelled hardware/pages."""
    cal = state.get('calibration', {}) if isinstance(state, dict) else {}
    marker = cal.get('commit_marker')
    if isinstance(marker, str) and ':' in marker:
        schema, digest = marker.split(':', 1)
        marker = {'schema': schema, 'digest': digest}
    pages = cal.get('pages', {})

    out = {
        'committed_schema': None,
        'marker': marker,
        'marker_valid': False,
        'committed_page_valid': False,
        'active_page': cal.get('active_page'),
        'staged_schema': None,
        'staged_valid': False,
        'transition_open': False,
        'errors': [],
    }
    if isinstance(marker, dict) and marker.get('schema') in CALIBRATION_SCHEMAS:
        out['committed_schema'] = marker['schema']
        page = pages.get(marker['schema'])
        digest = _page_digest(page)
        out['marker_valid'] = bool(
            digest and hmac.compare_digest(str(marker.get('digest')), digest)
        )
        out['committed_page_valid'] = (
            out['marker_valid']
            and validate_calibration(page).get('ok', False)
        )
        if not out['marker_valid']:
            out['errors'].append('commit_marker_digest_mismatch')

    for schema in ('K2', 'K1'):
        page = pages.get(schema)
        if not isinstance(page, dict):
            continue
        state_label = page.get('state')
        if schema != out['committed_schema'] or state_label == 'staged_uncommitted':
            if state_label in ('staged_uncommitted', 'staged', 'converting'):
                validation = validate_calibration(page)
                if schema == 'K2':
                    out['staged_schema'] = 'K2'
                    out['staged_valid'] = validation['ok']
                    if not validation['ok']:
                        out['errors'].extend(validation['errors'])
    out['transition_open'] = bool(
        out['staged_schema'] == 'K2'
        or str(cal.get('state', '')).startswith('converting')
        or str((cal.get('conversion') or {}).get('state', '')).startswith(
            ('staged', 'computed')
        )
    )
    return out


# ---------------------------------------------------------------------------
# Signed compatibility matrix
# ---------------------------------------------------------------------------

def default_matrix():
    return seal_document({
        'matrix_id': 'CM-FM-2026-Q4',
        'kind': 'cross_controller_compatibility_matrix',
        'entries': [
            {
                'id': 'CMB-v1-v1',
                'controllers': {ECU1: 'v1', ECU2: 'v1'},
                'calibration': 'K1',
                'status': 'certified',
                'functions': BASE_FUNCTIONS,
            },
            {
                'id': 'CMB-v2-v2',
                'controllers': {ECU1: 'v2', ECU2: 'v2'},
                'calibration': 'K2',
                'status': 'certified',
                'functions': V2_FUNCTIONS,
            },
        ],
    })


DEFAULT_MATRIX = default_matrix()


def verify_matrix(matrix, key=VENDOR_KEY):
    errors = []
    if not isinstance(matrix, dict):
        return {'ok': False, 'errors': ['matrix_not_object'], 'matrix_id': None}
    if not verify_seal(matrix, key):
        errors.append('matrix_signature_invalid')
    entries = matrix.get('entries')
    if not isinstance(entries, list) or not entries:
        errors.append('matrix_entries_missing')
    else:
        for i, entry in enumerate(entries):
            versions = entry_versions(entry)
            if not versions or not all(v for v in versions.values()):
                errors.append(f'matrix_entry_{i}_versions_invalid')
            if not entry.get('functions'):
                errors.append(f'matrix_entry_{i}_functions_missing')
    return {
        'ok': not errors,
        'errors': errors,
        'matrix_id': matrix.get('matrix_id'),
    }


def entry_versions(entry):
    if not isinstance(entry, dict):
        return {}
    raw = entry.get('controllers') or entry.get('versions')
    if isinstance(raw, dict):
        return dict(raw)
    combo = entry.get('combination')
    if isinstance(combo, dict):
        return dict(combo)
    if isinstance(combo, (list, tuple)):
        out = {}
        for item in combo:
            if isinstance(item, dict):
                out[item.get('controller_id')] = item.get('version')
            elif isinstance(item, (list, tuple)) and len(item) == 2:
                out[item[0]] = item[1]
        if out:
            return out
    return {k: entry[k] for k in CONTROLLERS if k in entry}


def lookup_matrix(versions_map, matrix=None):
    """Return the signed matrix entry exactly matching the controller combo."""
    matrix = matrix or DEFAULT_MATRIX
    if not verify_seal(matrix):
        return None
    wanted = {k: versions_map.get(k) for k in sorted(versions_map)}
    for entry in matrix.get('entries', []):
        candidate = entry_versions(entry)
        if {k: candidate.get(k) for k in sorted(candidate)} == wanted:
            return entry
    return None


compatibility_entry = lookup_matrix
matrix_entry_for = lookup_matrix


def compatible(versions_map, matrix=None):
    entry = lookup_matrix(versions_map, matrix)
    return bool(entry and entry.get('status', 'certified') == 'certified')


check_compatibility = compatible
is_compatible = compatible


# ---------------------------------------------------------------------------
# Recovery tokens (vendor signed, machine bound)
# ---------------------------------------------------------------------------

def issue_recovery_token(*, machine_id, action='downgrade_to_v1',
                         target_versions=None, transaction_id=None,
                         expires_at='2027-11-24T00:00:00',
                         calibration_rollback=True, key=VENDOR_KEY,
                         token_id='RT-FM07-0001'):
    """Vendor recovery token.  Downgrade is impossible without one."""
    target_versions = target_versions or {ECU1: 'v1', ECU2: 'v1'}
    token = {
        'token_id': token_id,
        'kind': 'vendor_recovery_token',
        'action': action,
        'machine_id': machine_id,
        'target_versions': dict(target_versions),
        'calibration_rollback': calibration_rollback,
        'transaction_id': transaction_id,
        'expires_at': expires_at,
        'issued_at': '2026-11-24T08:00:00',
    }
    return seal_document(token, key)


def verify_recovery_token(token, machine_id, *, now=FIXED_NOW,
                          action='downgrade_to_v1', key=VENDOR_KEY):
    errors = []
    checks = {
        'signature': False, 'machine_binding': False,
        'action': False, 'not_expired': False,
    }
    if not isinstance(token, dict):
        return {'ok': False, 'errors': ['recovery_token_missing'], 'checks': checks}
    checks['signature'] = verify_seal(token, key)
    if not checks['signature']:
        errors.append('recovery_token_signature_invalid')
    checks['machine_binding'] = token.get('machine_id') == machine_id
    if not checks['machine_binding']:
        errors.append('recovery_token_machine_mismatch')
    checks['action'] = token.get('action') == action
    if not checks['action']:
        errors.append('recovery_token_wrong_action')
    expires = token.get('expires_at')
    checks['not_expired'] = bool(expires) and (now is None or str(now) <= str(expires))
    if not checks['not_expired']:
        errors.append('recovery_token_expired')
    return {
        'ok': not errors,
        'errors': errors,
        'checks': checks,
        'token_id': token.get('token_id'),
        'machine_id': token.get('machine_id'),
        'target_versions': token.get('target_versions'),
        'calibration_rollback': token.get('calibration_rollback'),
        'transaction_id': token.get('transaction_id'),
    }


check_recovery_token = verify_recovery_token
verify_token = verify_recovery_token


def require_recovery_token(token, machine_id, **kwargs):
    report = verify_recovery_token(token, machine_id, **kwargs)
    if not report['ok']:
        raise RecoveryTokenError(
            'downgrade rejected: invalid recovery token',
            errors=report['errors'], report=report,
        )
    return report


def token_for_other_machine(token):
    """Test/helper: re-bind a valid token to another machine (must fail)."""
    fake = copy.deepcopy(token)
    fake['machine_id'] = 'SOME-OTHER-MACHINE'
    return fake


# ---------------------------------------------------------------------------
# Modelled dual-bank hardware + fixed breakpoint
# ---------------------------------------------------------------------------

def _page(schema, data, state):
    return {
        'schema': schema,
        'state': state,
        'data': data,
        'digest': document_digest(data),
    }


def breakpoint_state(machine_id=DEFAULT_MACHINE_ID, *, k2='valid',
                     transaction_id='TX-FM07-V2-0001'):
    """The fixed power-loss breakpoint described in the task.

    * ECU1 active/boot-verified on partition B containing v2; retained A=v1.
    * ECU2 active/boot-verified on A containing v1; B empty.
    * Calibration K1 remains the committed page; K1->K2 conversion is
      staged/uncommitted (job stage ``calibration_converting``).
    * The local upgrade job has no completion marker / is not closed.
    """
    k1 = default_k1(machine_id)
    pages = {'K1': _page('K1', k1, 'active_committed')}
    counters = {
        # The interrupted attempt already performed the K2 page program once.
        'calibration_program': 1 if k2 in ('valid', 'corrupt') else 0,
        'calibration_commit': 0,
        'calibration_discard': 0,
        'flash': 1,  # ECU1 v2 was flashed before the breakpoint
        'boot_confirm': 1,
    }
    cal = {
        'state': 'converting',
        'active_page': 'K1',
        'committed_page': 'K1',
        'commit_marker': {'schema': 'K1', 'digest': document_digest(k1)},
        'pages': pages,
        'conversion': {'from': 'K1', 'to': 'K2', 'state': 'staged_uncommitted'},
    }
    if k2 == 'valid':
        pages['K2'] = _page('K2', convert_calibration(k1), 'staged_uncommitted')
    elif k2 == 'corrupt':
        bad = dict(convert_calibration(k1))
        bad['gain_q16'] = int(bad['gain_q16']) + 7  # content vs digest mismatch
        pages['K2'] = _page('K2', bad, 'staged_uncommitted')
        # Deliberately corrupt stored bytes so digest validation fails.
        pages['K2']['digest'] = '0' * 64
    elif k2 in ('missing', 'none', None):
        cal['state'] = 'converting_before_page_write'
        cal['conversion']['state'] = 'computed_not_programmed'
        counters['calibration_program'] = 0
    else:
        raise ValueError(f'unknown k2 fixture mode: {k2!r}')

    return {
        'schema': SCHEMA_VERSION,
        'machine_id': machine_id,
        'readback_source': 'hardware_boot_control_block',
        'controllers': {
            ECU1: {
                'controller_id': ECU1,
                'role': CONTROLLER_ROLES[ECU1],
                'board_type': BOARD_TYPES[ECU1],
                'active_slot': 'B',
                'pending_slot': 'B',
                'slots': {
                    'A': {
                        'partition': 'A', 'version': 'v1',
                        'state': 'retained_good', 'retained': True,
                        'boot_verified': True, 'self_test': 'passed',
                    },
                    'B': {
                        'partition': 'B', 'version': 'v2',
                        'state': 'boot_verified',
                        'boot_verified': True, 'self_test': 'passed',
                    },
                },
            },
            ECU2: {
                'controller_id': ECU2,
                'role': CONTROLLER_ROLES[ECU2],
                'board_type': BOARD_TYPES[ECU2],
                'active_slot': 'A',
                'pending_slot': 'A',
                'slots': {
                    'A': {
                        'partition': 'A', 'version': 'v1',
                        'state': 'boot_verified',
                        'boot_verified': True, 'self_test': 'passed',
                    },
                    'B': {
                        'partition': 'B', 'version': None,
                        'state': 'empty', 'boot_verified': False,
                    },
                },
            },
        },
        'calibration': cal,
        'upgrade_job': {
            'job_id': 'JOB-FM07-V2',
            'transaction_id': transaction_id,
            'target': 'v2',
            'stage': 'calibration_converting',
            'completion_marker': False,
            'completed': False,
            'closed': False,
            'retry_cursor': {
                'controller_id': ECU1,
                'step': 'calibration_commit',
                'retry_count': 2,
            },
            'transaction_log': [
                {'seq': 1, 'op': 'packages_verified', 'result': 'ok'},
                {'seq': 2, 'op': 'ecu1_flash_v2_slot_B', 'result': 'ok'},
                {'seq': 3, 'op': 'ecu1_boot_self_test_B', 'result': 'passed'},
                {'seq': 4, 'op': 'calibration_K1_to_K2_program',
                 'result': 'power_lost_before_commit'},
            ],
        },
        'counters': counters,
        # Tests can inject failures: {"self_test_fail": ["ECU2"]}
        'fault_injection': {'self_test_fail': []},
    }


def build_default_packages(machine_id=DEFAULT_MACHINE_ID):
    return [
        build_package(controller_id=ECU1, version='v1',
                      image=f'FW1-ECU1-A1 retained vendor image {machine_id}\n'),
        build_package(controller_id=ECU1, version='v2',
                      image=f'FW2-ECU1-A1 amplitude vendor image {machine_id}\n'),
        build_package(controller_id=ECU2, version='v1',
                      image=f'FW1-ECU2-B2 work controller image {machine_id}\n'),
        build_package(controller_id=ECU2, version='v2',
                      image=f'FW2-ECU2-B2 amplitude work image {machine_id}\n'),
    ]


def index_packages(packages):
    out = {}
    for pkg in packages or []:
        if isinstance(pkg, dict):
            out[(pkg.get('controller_id'), pkg.get('version'))] = pkg
    return out


class HardwareBank:
    """Thin model of the two-controller board + calibration page hardware.

    Mutating methods represent device/driver operations.  ``state`` is plain
    JSON-able data so a power loss is simulated by dropping the object and
    constructing a new :class:`HardwareBank` from the persisted/readback dict.
    """

    def __init__(self, state=None, *, adopt=False, machine_id=DEFAULT_MACHINE_ID):
        if state is None:
            state = breakpoint_state(machine_id)
        self.state = state if adopt else copy.deepcopy(state)
        self.state.setdefault('counters', {})
        self.state.setdefault('fault_injection', {'self_test_fail': []})

    # -- loading / persistence --------------------------------------------
    @classmethod
    def load(cls, path):
        data = json.loads(Path(path).read_text(encoding='utf-8'))
        if isinstance(data, dict) and 'hardware' in data:
            data = data['hardware']
        return cls(data, adopt=True)

    def power_cycle(self):
        """Simulate power loss: only persisted/hardware state survives."""
        return HardwareBank(copy.deepcopy(self.state), adopt=True)

    # -- readback ----------------------------------------------------------
    def controller(self, controller_id):
        return self.state['controllers'][controller_id]

    def read_partitions(self, controller_id):
        c = self.controller(controller_id)
        active = c['active_slot']
        pending = c.get('pending_slot')
        active_slot = c.get('slots', {}).get(active, {})
        pending_slot = c.get('slots', {}).get(pending, {})
        return {
            'controller_id': controller_id,
            'board_type': c.get('board_type'),
            'active_partition': active,
            'pending_partition': pending,
            'active_slot': active,
            'pending_slot': pending,
            'pending_boot_partition': pending,
            'active_version': active_slot.get('version'),
            'running_version': active_slot.get('version'),
            'active_boot_verified': bool(active_slot.get('boot_verified')),
            'pending_version': pending_slot.get('version'),
            'slots': copy.deepcopy(c.get('slots', {})),
            'readback': 'hardware_boot_control_block',
        }

    def versions(self):
        return {
            cid: self.read_partitions(cid)['active_version']
            for cid in self.state.get('controllers', {})
        }

    # -- calibration operations -------------------------------------------
    def program_calibration_page(self, schema, data):
        """The irreversible calibration page program.  Caller guarantees once."""
        pages = self.state['calibration'].setdefault('pages', {})
        pages[schema] = _page(schema, data, 'staged_uncommitted')
        self.state['counters']['calibration_program'] = (
            self.state['counters'].get('calibration_program', 0) + 1
        )
        self._log(f'calibration_program_{schema}')

    def commit_calibration_marker(self, schema, digest):
        cal = self.state['calibration']
        cal['commit_marker'] = {'schema': schema, 'digest': digest}
        cal['active_page'] = schema
        cal['committed_page'] = schema
        cal['state'] = f'{schema}_committed'
        conversion = cal.get('conversion')
        if isinstance(conversion, dict):
            conversion['state'] = 'committed'
        page = cal.get('pages', {}).get(schema)
        if isinstance(page, dict):
            page['state'] = 'active_committed'
        other = 'K1' if schema == 'K2' else 'K2'
        opage = cal.get('pages', {}).get(other)
        if isinstance(opage, dict) and opage.get('state') == 'staged_uncommitted':
            opage['state'] = 'retained_uncommitted'
        self.state['counters']['calibration_commit'] = (
            self.state['counters'].get('calibration_commit', 0) + 1
        )
        self._log(f'calibration_commit_{schema}')

    def discard_staged_page(self, schema='K2'):
        cal = self.state['calibration']
        page = cal.get('pages', {}).get(schema)
        if page is not None:
            page['state'] = 'discarded_rollback'
        if cal.get('committed_page') == 'K1':
            cal['state'] = 'rolled_back_K1_active'
        conversion = cal.get('conversion')
        if isinstance(conversion, dict):
            conversion['state'] = 'rolled_back_to_K1'
        self.state['counters']['calibration_discard'] = (
            self.state['counters'].get('calibration_discard', 0) + 1
        )
        self._log(f'calibration_discard_{schema}')

    # -- flash / boot operations ------------------------------------------
    def flash_slot(self, controller_id, slot, package):
        c = self.controller(controller_id)
        c.setdefault('slots', {})[slot] = {
            'partition': slot,
            'version': package['version'],
            'package_id': package.get('package_id'),
            'sha256': package.get('sha256'),
            'state': 'written_unverified',
            'boot_verified': False,
            'self_test': 'not_run',
        }
        self.state['counters']['flash'] = (
            self.state['counters'].get('flash', 0) + 1
        )
        self._log(f'flash_{controller_id}_slot_{slot}_{package["version"]}')

    def arm_slot(self, controller_id, slot):
        self.controller(controller_id)['pending_slot'] = slot
        self._log(f'arm_pending_{controller_id}_slot_{slot}')

    def run_self_test(self, controller_id):
        faults = self.state.get('fault_injection', {})
        fail_set = set(faults.get('self_test_fail', []))
        c = self.controller(controller_id)
        pending = c.get('pending_slot')
        slot = c['slots'][pending]
        forced = controller_id in fail_set or faults.get(controller_id) == 'fail'
        if forced:
            slot['state'] = 'boot_failed'
            slot['boot_verified'] = False
            slot['self_test'] = 'failed'
            self._log(f'self_test_{controller_id}_slot_{pending}_FAILED')
            return False
        slot['state'] = 'boot_verified'
        slot['boot_verified'] = True
        slot['self_test'] = 'passed'
        c['active_slot'] = pending
        self.state['counters']['boot_confirm'] = (
            self.state['counters'].get('boot_confirm', 0) + 1
        )
        self._log(f'self_test_{controller_id}_slot_{pending}_PASSED')
        return True

    # -- job marker --------------------------------------------------------
    def close_job(self, *, outcome, marker):
        job = self.state['upgrade_job']
        job['stage'] = 'closed'
        job['closed'] = True
        job['completed'] = True
        job['completion_marker'] = marker
        job['outcome'] = outcome
        self._log(f'job_closed_{outcome}')

    def _log(self, op):
        log = self.state.setdefault('upgrade_job', {}).setdefault(
            'transaction_log', []
        )
        log.append({'seq': len(log) + 1, 'op': op, 'result': 'ok'})


def _hardware_like(doc):
    controllers = doc.get('controllers') if isinstance(doc, dict) else None
    return (
        isinstance(controllers, dict)
        and all(isinstance(c, dict) and 'slots' in c
                for c in controllers.values())
    )


def hardware_state_from_reports(events, machine_id=DEFAULT_MACHINE_ID):
    """Project simple version/boot reports into minimal readback state.

    Missing boot evidence is treated as *not* boot-verified, preserving the
    safety direction of the full proof.
    """
    controllers = {}
    for event in events or []:
        if not isinstance(event, dict) or 'controller_id' not in event:
            continue
        cid = event['controller_id']
        from .firmware import boot_verified as _boot
        boot = _boot(event)
        verified = boot is True
        letter = event.get('active_partition') or event.get('slot') or 'A'
        board = event.get('board_type') or BOARD_TYPES.get(cid, 'GENERIC-BOARD')
        controllers[cid] = {
            'controller_id': cid,
            'role': CONTROLLER_ROLES.get(cid, 'unknown'),
            'board_type': board,
            'active_slot': letter,
            'pending_slot': event.get('pending_partition') or letter,
            'slots': {
                letter: {
                    'partition': letter,
                    'version': event.get('version'),
                    'state': 'boot_verified' if verified else 'report_only',
                    'boot_verified': verified,
                    'self_test': 'passed' if verified else (
                        'failed' if boot is False else 'unknown'
                    ),
                }
            },
        }
    return {
        'schema': SCHEMA_VERSION,
        'machine_id': machine_id,
        'readback_source': 'version_report_projection',
        'controllers': controllers,
        'calibration': {'commit_marker': None, 'pages': {}},
        'upgrade_job': {'closed': False, 'completion_marker': False,
                        'stage': 'report_only'},
        'counters': {},
        'fault_injection': {'self_test_fail': []},
    }


def as_hardware(source=None):
    """Coerce HardwareBank | state-dict | fixture-doc | path -> HardwareBank."""
    if isinstance(source, HardwareBank):
        return source
    if source is None:
        return HardwareBank()
    if isinstance(source, (str, Path)):
        return HardwareBank.load(source)
    if isinstance(source, (list, tuple)):
        return HardwareBank(hardware_state_from_reports(source))
    if isinstance(source, dict):
        if 'hardware' in source and isinstance(source['hardware'], dict) \
                and 'controllers' in source['hardware']:
            return HardwareBank(source['hardware'], adopt=True)
        if _hardware_like(source):
            return HardwareBank(source, adopt=True)
        # Report-shaped: {"events": [...]}, {"controllers": [events]},
        # or a plain {controller_id: version} map.
        events = None
        if isinstance(source.get('events'), list):
            events = source['events']
        elif isinstance(source.get('controllers'), list):
            events = source['controllers']
        elif source and all(isinstance(v, str) for v in source.values()) \
                and any(k.startswith('ECU') for k in source):
            events = [{'controller_id': k, 'version': v}
                      for k, v in source.items()]
        if events is not None:
            return HardwareBank(hardware_state_from_reports(
                events, source.get('machine_id', DEFAULT_MACHINE_ID)
                if isinstance(source, dict) else DEFAULT_MACHINE_ID
            ))
    raise TypeError(f'cannot read hardware state from {type(source)!r}')


# ---------------------------------------------------------------------------
# Capture / readback evidence
# ---------------------------------------------------------------------------

def capture(source=None):
    """Produce an offline evidence snapshot strictly from hardware readback."""
    hw = as_hardware(source)
    state = hw.state
    partitions = {cid: hw.read_partitions(cid)
                  for cid in state.get('controllers', {})}
    cal_status = calibration_status(state)
    job = state.get('upgrade_job', {})
    return {
        'kind': 'firmware_capture',
        'captured_from': 'hardware_readback',
        'readback': True,
        'machine_id': state.get('machine_id'),
        'partitions': partitions,
        # Alias some callers may look for.
        'dual_partition': partitions,
        'controller_versions': {
            cid: p['active_version'] for cid, p in partitions.items()
        },
        'versions': {
            cid: p['active_version'] for cid, p in partitions.items()
        },
        'boot_verified': {
            cid: bool(state['controllers'][cid]['slots']
                      .get(state['controllers'][cid]['active_slot'], {})
                      .get('boot_verified'))
            for cid in partitions
        },
        'calibration': {
            'committed_schema': cal_status['committed_schema'],
            'active_page': cal_status['active_page'],
            'staged_schema': cal_status['staged_schema'],
            'staged_valid': cal_status['staged_valid'],
            'transition_open': cal_status['transition_open'],
            'marker_valid': cal_status['marker_valid'],
            'errors': cal_status['errors'],
        },
        'calibration_detail': cal_status,
        'upgrade_job': {
            'transaction_id': job.get('transaction_id'),
            'stage': job.get('stage'),
            'closed': bool(job.get('closed')),
            'completion_marker': job.get('completion_marker'),
            'retry_cursor': job.get('retry_cursor'),
        },
        'counters': copy.deepcopy(state.get('counters', {})),
    }


capture_state = capture
read_hardware = capture
readback = capture
hardware_readback = capture
snapshot = capture
collect_state = capture


def read_partitions(source=None, controller_id=None):
    """Module-level hardware readback for one board or every board."""
    hw = as_hardware(source)
    if controller_id is not None:
        return hw.read_partitions(controller_id)
    return {cid: hw.read_partitions(cid)
            for cid in hw.state.get('controllers', {})}


read_partition_state = read_partitions
read_boot_state = read_partitions
partition_readback = read_partitions


def recovery_proof(source=None, **kwargs):
    """A JSON-serializable offline proof bundle for the current state."""
    verdict = assess(source, **kwargs)
    return {
        'kind': 'firmware_recovery_proof',
        'machine_id': verdict['machine_id'],
        'captured': capture(source) if not isinstance(source, HardwareBank)
        else capture(source),
        'verdict': verdict,
        'assessment': verdict,
        'breakpoint': verdict['breakpoint'],
        'safe_functions': verdict['functions'],
        'can_dispatch': verdict['can_dispatch'],
        'scope': SCOPE,
    }


prove = recovery_proof
build_proof = recovery_proof


# ---------------------------------------------------------------------------
# Main verdict: safe function set + dispatch readiness + recovery breakpoint
# ---------------------------------------------------------------------------

def unreadable_verdict(source, error=None, *, now=FIXED_NOW):
    """Conservative verdict used when no hardware readback can be obtained."""
    return {
        'kind': 'firmware_roadworthy_verdict',
        'machine_id': None,
        'status': 'grounded',
        'roadworthy': False,
        'ready': False,
        'ok': False,
        'can_dispatch': False,
        'dispatchable': False,
        'dispatch': False,
        'can_return_to_queue': False,
        'return_to_queue': False,
        'work_queue_ready': False,
        'grounded': True,
        'stopped': True,
        'controller_versions': {},
        'versions': {},
        'combination': (None, None),
        'combination_map': {},
        'matrix_id': None,
        'matrix_entry': None,
        'matrix_entry_id': None,
        'functions': [],
        'safe_functions': [],
        'available_functions': [],
        'enabled_functions': [],
        'unavailable_functions': list(FUNCTION_CATALOG),
        'disabled_functions': list(FUNCTION_CATALOG),
        'eligible_tasks': [],
        'scheduling': {
            'can_dispatch': False, 'eligible_task_types': [],
            'accepted_task_types': [],
            'amplitude_control_available': False,
            'variable_width_available': False,
            'restrictions': ['no_hardware_readback'],
        },
        'checks': {'hardware_readback': False},
        'reasons': ['hardware_readback_unavailable'],
        'reason_codes': ['hardware_readback_unavailable'],
        'breakpoint': None,
        'checkpoint': None,
        'scope': SCOPE,
        'assessed_at': now,
        'input_error': str(error) if error else None,
    }


def assess(source=None, *, matrix=None, now=FIXED_NOW,
           controllers=CONTROLLERS, **_):
    """Decide the currently safe function set and whether the machine rolls.

    The verdict is conservative: unless the signed matrix certifies the exact
    active version combination, every active partition is boot-verified, the
    calibration schema required by that combination is atomically committed
    and the upgrade transaction is closed, the machine is grounded with an
    explicit resumable breakpoint and an empty safe-function set.
    """
    try:
        hw = as_hardware(source)
    except Exception as exc:  # no readback -> never claim ready
        return unreadable_verdict(source, exc, now=now)
    state = hw.state
    matrix_doc = DEFAULT_MATRIX if matrix is None else matrix
    matrix_report = verify_matrix(matrix_doc)

    reasons = []
    checks = {
        'matrix_signature': matrix_report['ok'],
        'all_controllers_present': True,
        'boot_verified': True,
        'combination_listed': False,
        'calibration_committed': False,
        'calibration_schema_match': False,
        'transaction_closed': False,
    }
    if not matrix_report['ok']:
        reasons.extend(matrix_report['errors'])

    versions_map = {}
    partitions = {}
    for cid in controllers:
        c = state.get('controllers', {}).get(cid)
        if c is None:
            checks['all_controllers_present'] = False
            reasons.append(f'controller_missing:{cid}')
            continue
        p = hw.read_partitions(cid)
        partitions[cid] = p
        versions_map[cid] = p['active_version']
        active_slot = c.get('slots', {}).get(c.get('active_slot'), {})
        if not active_slot.get('boot_verified') \
                or active_slot.get('self_test') in ('failed',):
            checks['boot_verified'] = False
            reasons.append(f'{cid}_active_partition_not_boot_verified')

    entry = None
    if matrix_report['ok'] and checks['all_controllers_present']:
        entry = lookup_matrix(versions_map, matrix_doc)
        checks['combination_listed'] = entry is not None
        if entry is None:
            reasons.append('mixed_combination_unconfirmed')

    cal_status = calibration_status(state)
    required_schema = (entry or {}).get('calibration')
    if entry is not None and required_schema:
        schema_match = cal_status['committed_schema'] == required_schema
        checks['calibration_schema_match'] = schema_match
        checks['calibration_committed'] = bool(
            schema_match and cal_status['marker_valid']
            and cal_status['committed_page_valid']
        )
        if not schema_match:
            reasons.append(
                f'calibration_commit_pending:committed='
                f'{cal_status["committed_schema"]},required={required_schema}'
            )
        elif not cal_status['marker_valid']:
            reasons.append('calibration_commit_marker_invalid')
    else:
        # No certified combination: still surface an interrupted conversion.
        if cal_status['transition_open']:
            reasons.append('calibration_transition_interrupted')
        if cal_status['errors']:
            reasons.append('calibration_page_invalid')

    job = state.get('upgrade_job', {})
    marker = job.get('completion_marker')
    closed = bool(job.get('closed')) or (
        marker not in (False, None) and job.get('completed', True) is not False
    )
    checks['transaction_closed'] = closed
    if not closed:
        reasons.append('upgrade_transaction_open')

    roadworthy = all(checks.values()) and entry is not None

    if roadworthy:
        functions = list(entry.get('functions', []))
        matrix_entry_id = entry.get('id')
        unavailable = [f for f in FUNCTION_CATALOG if f not in functions
                       and f not in ('variable_width_control',)]
        task_types = []
        for f in functions:
            for t in FUNCTION_CATALOG.get(f, {}).get('task_types', []):
                if t not in task_types:
                    task_types.append(t)
        scheduling = {
            'can_dispatch': True,
            'eligible_task_types': task_types,
            'accepted_task_types': task_types,
            'amplitude_control_available': 'amplitude_control' in functions,
            'variable_width_available': 'amplitude_control' in functions,
            'restrictions': [] if 'amplitude_control' in functions
            else ['amplitude_control_unavailable'],
        }
        breakpoint_doc = None
        status = 'roadworthy'
    else:
        functions = []
        unavailable = [f for f in FUNCTION_CATALOG
                       if f not in ('variable_width_control',)]
        scheduling = {
            'can_dispatch': False,
            'eligible_task_types': [],
            'accepted_task_types': [],
            'amplitude_control_available': False,
            'variable_width_available': False,
            'restrictions': ['machine_grounded_pending_recovery'],
        }
        status = 'grounded'
        breakpoint_doc = build_breakpoint(
            state, versions_map=versions_map, partitions=partitions,
            reasons=reasons, matrix_id=(matrix_doc or {}).get('matrix_id'),
        )

    verdict = {
        'kind': 'firmware_roadworthy_verdict',
        'machine_id': state.get('machine_id'),
        'status': status,
        'roadworthy': roadworthy,
        'ready': roadworthy,
        'ok': roadworthy,
        'can_dispatch': roadworthy,
        'dispatchable': roadworthy,
        'dispatch': roadworthy,
        'can_return_to_queue': roadworthy,
        'return_to_queue': roadworthy,
        'work_queue_ready': roadworthy,
        'grounded': not roadworthy,
        'stopped': not roadworthy,
        'controller_versions': versions_map,
        'versions': versions_map,
        'combination': tuple(versions_map.get(c) for c in controllers),
        'combination_map': dict(versions_map),
        'matrix_id': (matrix_doc or {}).get('matrix_id'),
        'matrix_entry': entry,
        'matrix_entry_id': (entry or {}).get('id'),
        'functions': functions,
        'safe_functions': functions,
        'safe_function_set': functions,
        'available_functions': functions,
        'enabled_functions': functions,
        'functions_zh': [
            FUNCTION_CATALOG[f]['zh']
            for f in functions
            if isinstance(FUNCTION_CATALOG.get(f), dict)
        ],
        'unavailable_functions': unavailable,
        'disabled_functions': unavailable,
        'eligible_tasks': scheduling['eligible_task_types'],
        'scheduling': scheduling,
        'checks': checks,
        'reasons': reasons,
        'reason_codes': reasons,
        'breakpoint': breakpoint_doc,
        'checkpoint': breakpoint_doc,
        'recovery_breakpoint': breakpoint_doc,
        'scope': SCOPE,
        'assessed_at': now,
    }
    return verdict


evaluate = assess
diagnose = assess


def is_roadworthy(source=None, **kwargs):
    return bool(assess(source, **kwargs)['roadworthy'])


roadworthy = is_roadworthy


def build_breakpoint(state, *, versions_map=None, partitions=None,
                     reasons=None, matrix_id=None):
    """Construct the resumable recovery checkpoint for a grounded machine."""
    hw = as_hardware(state) if not isinstance(state, HardwareBank) else state
    state = hw.state
    if versions_map is None:
        versions_map = hw.versions()
    job = state.get('upgrade_job', {})
    return {
        'checkpoint_id': f"{job.get('transaction_id')}#{job.get('stage')}",
        'machine_id': state.get('machine_id'),
        'stage': job.get('stage'),
        'transaction_id': job.get('transaction_id'),
        'retry_cursor': copy.deepcopy(job.get('retry_cursor')),
        'controller_versions': versions_map,
        'combination': [versions_map.get(c) for c in CONTROLLERS],
        'partitions': {
            cid: (partitions or {}).get(cid) or hw.read_partitions(cid)
            for cid in state.get('controllers', {})
        },
        'calibration': calibration_status(state),
        'upgrade_job': {
            'stage': job.get('stage'),
            'closed': bool(job.get('closed')),
            'completion_marker': job.get('completion_marker'),
        },
        'reasons': list(reasons or []),
        'matrix_id': matrix_id,
        'resume_options': {
            'forward_v2': {
                'description_zh': '提交可验证 K2 校准，升级 ECU2 到 v2 并逐板启动自检',
                'steps': [
                    'verify_k2_commit',
                    'flash_ECU2_v2',
                    'arm_and_boot_verify_ECU2',
                    'write_completion_marker',
                ],
            },
            'downgrade_v1': {
                'description_zh': '凭本机厂商恢复令牌从 ECU1 保留分区恢复 v1，回退未提交 K2',
                'requires': 'vendor_recovery_token_bound_to_machine',
                'steps': [
                    'verify_recovery_token',
                    'restore_ECU1_retained_A_v1',
                    'rollback_uncommitted_K2',
                    'boot_verify_ECU1',
                    'close_rollback_job',
                ],
            },
        },
    }
    bp['recovery_options'] = bp['resume_options']
    return bp


recovery_breakpoint = build_breakpoint


# ---------------------------------------------------------------------------
# Calibration step used by the forward route
# ---------------------------------------------------------------------------

def ensure_k2_committed(hw):
    """Idempotently reach a valid committed K2 calibration.

    Returns evidence about what this invocation did.  Raises
    :class:`RecoveryHalted` on an unverifiable/corrupt K2 page; it never
    blindly re-programmes a page whose irreversible write already happened.
    """
    state = hw.state
    cal = state['calibration']
    status = calibration_status(state)

    if status['committed_schema'] == 'K2' and status['marker_valid']:
        return {'action': 'skip_already_committed', 'calibration_writes': 0}

    k2_page = cal.get('pages', {}).get('K2')
    if isinstance(k2_page, dict):
        validation = validate_calibration(k2_page)
        digest_ok = hmac.compare_digest(
            str(k2_page.get('digest', '')),
            document_digest(k2_page.get('data', {})),
        )
        if validation['ok'] and digest_ok:
            # The irreversible program already survived power loss: commit only.
            hw.commit_calibration_marker('K2', k2_page['digest'])
            return {'action': 'commit_staged_valid_K2',
                    'calibration_writes': 0}
        # A half-written / tampered K2 page: stop safe, do not rewrite.
        raise RecoveryHalted(
            'calibration_page_corrupt',
            stage='calibration_commit',
            reasons=['calibration_page_corrupt'] + validation['errors'],
        )

    # K2 was never physically programmed: derive it from intact committed K1.
    k1_page = cal.get('pages', {}).get('K1')
    k1 = (k1_page or {}).get('data')
    validation = validate_calibration(k1, schema='K1')
    if not validation['ok']:
        raise RecoveryHalted(
            'calibration_k1_invalid', stage='calibration_convert',
            reasons=['calibration_k1_invalid'] + validation['errors'],
        )
    k2 = convert_calibration(k1)
    hw.program_calibration_page('K2', k2)  # the single irreversible write
    digest = hw.state['calibration']['pages']['K2']['digest']
    hw.commit_calibration_marker('K2', digest)
    return {'action': 'program_and_commit_K2', 'calibration_writes': 1}


def rollback_uncommitted_k2(hw, token_report=None):
    """Discard uncommitted K2 and leave committed K1 untouched."""
    status = calibration_status(hw.state)
    if status['committed_schema'] == 'K1' and status['marker_valid']:
        k2 = hw.state['calibration'].get('pages', {}).get('K2')
        if isinstance(k2, dict) and k2.get('state') != 'discarded_rollback':
            hw.discard_staged_page('K2')
            return {'action': 'discard_uncommitted_K2', 'calibration_writes': 0}
        return {'action': 'K2_already_discarded', 'calibration_writes': 0}

    if status['committed_schema'] == 'K2':
        allowed = bool((token_report or {}).get('calibration_rollback'))
        k1_page = hw.state['calibration'].get('pages', {}).get('K1')
        k1_ok = validate_calibration(k1_page).get('ok', False)
        if not allowed or not k1_ok:
            raise RecoveryHalted(
                'calibration_committed_K2_rollback_not_authorized',
                stage='calibration_rollback',
            )
        digest = _page_digest(k1_page)
        hw.commit_calibration_marker('K1', digest)
        return {'action': 'token_authorized_rollback_to_K1',
                'calibration_writes': 0}

    raise RecoveryHalted('calibration_K1_page_unusable',
                        stage='calibration_rollback')


# ---------------------------------------------------------------------------
# Boot slot helper
# ---------------------------------------------------------------------------

def ensure_boot_version(hw, controller_id, version, *, prefer_slot=None):
    """Arm a slot containing ``version`` and pass its boot self-test."""
    partitions = hw.read_partitions(controller_id)
    if (partitions['active_version'] == version
            and partitions['active_boot_verified']):
        return {'action': 'skip_already_boot_verified',
                'slot': partitions['active_slot']}

    c = hw.controller(controller_id)
    candidate = None
    if prefer_slot and c['slots'].get(prefer_slot, {}).get('version') == version:
        candidate = prefer_slot
    else:
        for slot_id, slot in c.get('slots', {}).items():
            if slot.get('version') == version and slot.get('state') in (
                'retained_good', 'boot_verified', 'written_unverified'
            ):
                candidate = slot_id
                break
    if candidate is None:
        raise RecoveryHalted(
            f'{controller_id}_no_bootable_{version}_slot',
            stage=f'{controller_id}_restore_{version}',
        )
    hw.arm_slot(controller_id, candidate)
    if not hw.run_self_test(controller_id):
        raise RecoveryHalted(
            f'{controller_id}_boot_self_test_failed',
            stage=f'{controller_id}_boot_self_test',
        )
    return {'action': 'armed_and_boot_verified', 'slot': candidate}


# ---------------------------------------------------------------------------
# Recovery route A: machine-token downgrade to v1
# ---------------------------------------------------------------------------

def route_downgrade(source=None, *, token=None, matrix=None, now=FIXED_NOW,
                    strict=False, ledger=None, issued_at=FIXED_NOW,
                    conclusion_id=None, packages=None, **_):
    """Vendor-token downgrade: ECU1 retained A/v1 + K2 rollback.

    Ends at certified (v1, v1) / K1: roadworthy for basic tillage only.
    """
    hw = as_hardware(source)
    machine_id = hw.state.get('machine_id')
    # Allow the whole fixture document to supply its token/packages.
    if token is None and isinstance(source, dict):
        token = source.get('recovery_token')
    trail = []
    matrix_doc = DEFAULT_MATRIX if matrix is None else matrix

    def record(step, evidence):
        item = {'step': step, 'status': 'ok'}
        item.update(evidence or {})
        trail.append(item)

    try:
        token_report = verify_recovery_token(
            token, machine_id, now=now, action='downgrade_to_v1'
        )
        record('verify_recovery_token', {
            'ok': token_report['ok'], 'token_id': token_report.get('token_id'),
            'errors': token_report['errors'],
        })
        if not token_report['ok']:
            raise RecoveryHalted(
                'recovery_token_invalid', stage='verify_recovery_token',
                reasons=token_report['errors'], token_report=token_report,
            )

        # Retained v1 partition must exist before touching calibration.
        ecu1 = hw.controller(ECU1)
        retained = [
            sid for sid, slot in ecu1.get('slots', {}).items()
            if slot.get('version') == 'v1' and slot.get('retained')
            and slot.get('state') in ('retained_good', 'boot_verified')
        ]
        if not retained:
            raise RecoveryHalted(
                'retained_v1_partition_missing',
                stage='restore_ECU1_retained_v1',
            )
        record('locate_ECU1_retained_v1', {'slots': retained})

        cal_evidence = rollback_uncommitted_k2(hw, token_report)
        record('rollback_uncommitted_K2', cal_evidence)

        boot1 = ensure_boot_version(hw, ECU1, 'v1', prefer_slot=retained[0])
        record('restore_ECU1_retained_A_v1', boot1)

        boot2 = ensure_boot_version(hw, ECU2, 'v1', prefer_slot='A')
        record('ensure_ECU2_v1_boot_verified', boot2)

        hw.close_job(outcome='downgraded_v1', marker={
            'type': 'recovery_completion',
            'route': 'downgrade_v1',
            'token_id': token_report.get('token_id'),
            'controller_versions': hw.versions(),
        })
        record('write_recovery_completion_marker', {'route': 'downgrade_v1'})
    except RecoveryHalted as halt:
        verdict = assess(hw.state, matrix=matrix_doc, now=now)
        bp = verdict['breakpoint'] or build_breakpoint(hw.state)
        bp['failed_step'] = halt.stage
        bp['failure_code'] = halt.code
        result = {
            'kind': 'recovery_result',
            'route': 'downgrade_v1',
            'path': 'downgrade_v1',
            'ok': False,
            'success': False,
            'roadworthy': False,
            'can_dispatch': False,
            'dispatchable': False,
            'halted': True,
            'trail': trail,
            'steps': trail,
            'breakpoint': bp,
            'checkpoint': bp,
            'assessment': verdict,
            'verdict': verdict,
            'state': hw.state,
            'hardware': hw,
        }
        if strict:
            raise
        return result

    verdict = assess(hw.state, matrix=matrix_doc, now=now)
    result = {
        'kind': 'recovery_result',
        'route': 'downgrade_v1',
        'path': 'downgrade_v1',
        'ok': verdict['roadworthy'],
        'success': verdict['roadworthy'],
        'roadworthy': verdict['roadworthy'],
        'can_dispatch': verdict['can_dispatch'],
        'dispatchable': verdict['dispatchable'],
        'halted': not verdict['roadworthy'],
        'trail': trail,
        'steps': trail,
        'breakpoint': verdict['breakpoint'],
        'checkpoint': verdict['breakpoint'],
        'assessment': verdict,
        'verdict': verdict,
        'state': hw.state,
        'hardware': hw,
    }
    if ledger is not None and verdict['roadworthy']:
        ledger.add_conclusion(
            verdict, conclusion_id=conclusion_id, route='downgrade_v1',
            issued_at=issued_at,
            summary_zh='凭本机恢复令牌回退 v1/K1：仅可基础耕作，不可使用变幅控制。',
        )
    if strict and not result['ok']:
        raise RecoveryHalted('downgrade_did_not_reach_roadworthy')
    return result


downgrade = route_downgrade
restore_v1 = route_downgrade
apply_downgrade = route_downgrade
recover_with_token = route_downgrade


# ---------------------------------------------------------------------------
# Recovery route B: commit K2, upgrade ECU2 v2, boot verify both
# ---------------------------------------------------------------------------

def route_forward(source=None, *, package=None, packages=None, matrix=None,
                  now=FIXED_NOW, strict=False, ledger=None, issued_at=FIXED_NOW,
                  conclusion_id=None, target_version='v2', **_):
    """Forward recovery: verifiable K2 commit then ECU2 v2 + boot self-test."""
    hw = as_hardware(source)
    machine_id = hw.state.get('machine_id')
    if packages is None and isinstance(source, dict):
        packages = source.get('packages')
    matrix_doc = DEFAULT_MATRIX if matrix is None else matrix
    trail = []

    def record(step, evidence):
        item = {'step': step, 'status': 'ok'}
        item.update(evidence or {})
        trail.append(item)

    try:
        # Resolve + verify the ECU2 target package before any mutation.
        if package is None:
            idx = index_packages(packages)
            package = idx.get((ECU2, target_version))
        if package is None:
            package = build_package(controller_id=ECU2, version=target_version)
        board = hw.controller(ECU2)['board_type']
        pkg_report = verify_package(package, board_type=board)
        record('verify_ECU2_package_triple', {
            'ok': pkg_report['ok'], 'errors': pkg_report['errors'],
            'checks': pkg_report['checks'],
        })
        if not pkg_report['ok']:
            raise RecoveryHalted(
                'package_verification_failed',
                stage='verify_ECU2_package',
                reasons=pkg_report['errors'], report=pkg_report,
            )

        cal_evidence = ensure_k2_committed(hw)
        record('complete_verifiable_K2_commit', cal_evidence)

        # Idempotent ECU2 flash/boot.
        partitions = hw.read_partitions(ECU2)
        if not (partitions['active_version'] == target_version
                and partitions['active_boot_verified']):
            inactive = None
            active_letter = hw.controller(ECU2)['active_slot']
            for letter in ('A', 'B'):
                if letter != active_letter:
                    inactive = letter
                    break
            existing = hw.controller(ECU2)['slots'].get(inactive, {})
            already = (
                existing.get('version') == target_version
                and existing.get('sha256') == package.get('sha256')
            )
            if not already:
                hw.flash_slot(ECU2, inactive, package)
                record('flash_ECU2_v2', {'slot': inactive})
            else:
                record('flash_ECU2_v2_skipped_same_bytes', {'slot': inactive})
            boot = ensure_boot_version(hw, ECU2, target_version,
                                       prefer_slot=inactive)
            record('boot_self_test_ECU2_v2', boot)
        else:
            record('ECU2_already_v2_boot_verified',
                   {'slot': partitions['active_slot']})

        # ECU1 must still be the boot-verified v2 from the breakpoint.
        ensure_boot_version(hw, ECU1, target_version, prefer_slot='B')
        record('confirm_ECU1_v2_boot_verified',
               hw.read_partitions(ECU1))

        hw.close_job(outcome='forward_v2', marker={
            'type': 'recovery_completion',
            'route': 'forward_v2',
            'target': target_version,
            'controller_versions': hw.versions(),
            'calibration': 'K2',
        })
        record('write_completion_marker', {'route': 'forward_v2'})
    except RecoveryHalted as halt:
        verdict = assess(hw.state, matrix=matrix_doc, now=now)
        bp = verdict['breakpoint'] or build_breakpoint(hw.state)
        bp['failed_step'] = halt.stage
        bp['failure_code'] = halt.code
        result = {
            'kind': 'recovery_result',
            'route': 'forward_v2',
            'path': 'forward_v2',
            'ok': False,
            'success': False,
            'roadworthy': False,
            'can_dispatch': False,
            'dispatchable': False,
            'halted': True,
            'trail': trail,
            'steps': trail,
            'breakpoint': bp,
            'checkpoint': bp,
            'assessment': verdict,
            'verdict': verdict,
            'state': hw.state,
            'hardware': hw,
        }
        if strict:
            raise
        return result

    verdict = assess(hw.state, matrix=matrix_doc, now=now)
    result = {
        'kind': 'recovery_result',
        'route': 'forward_v2',
        'path': 'forward_v2',
        'ok': verdict['roadworthy'],
        'success': verdict['roadworthy'],
        'roadworthy': verdict['roadworthy'],
        'can_dispatch': verdict['can_dispatch'],
        'dispatchable': verdict['dispatchable'],
        'halted': not verdict['roadworthy'],
        'trail': trail,
        'steps': trail,
        'breakpoint': verdict['breakpoint'],
        'checkpoint': verdict['breakpoint'],
        'assessment': verdict,
        'verdict': verdict,
        'state': hw.state,
        'hardware': hw,
    }
    if ledger is not None and verdict['roadworthy']:
        ledger.add_conclusion(
            verdict, conclusion_id=conclusion_id, route='forward_v2',
            issued_at=issued_at,
            summary_zh='K2 已提交且两板均为启动验证 v2：可出车，可用变幅控制。',
        )
    if strict and not result['ok']:
        raise RecoveryHalted('forward_did_not_reach_roadworthy')
    return result


forward = route_forward
upgrade = route_forward
apply_forward = route_forward
complete_forward_recovery = route_forward


def recover(source=None, route='forward_v2', **kwargs):
    if route in ('forward_v2', 'forward', 'upgrade_v2'):
        return route_forward(source, **kwargs)
    if route in ('downgrade_v1', 'downgrade', 'token_downgrade'):
        return route_downgrade(source, **kwargs)
    raise ValueError(f'unknown recovery route: {route!r}')


def resume(source=None, route=None, **kwargs):
    """Re-enter either route from a persisted breakpoint, idempotently."""
    if route is None and isinstance(source, dict):
        route = source.get('recommended_route') or 'forward_v2'
    return recover(source, route=route or 'forward_v2', **kwargs)


def plan_recovery(source=None, **kwargs):
    """Return the legal recovery options for the current breakpoint."""
    verdict = assess(source, **kwargs)
    if verdict['roadworthy']:
        return {'status': 'roadworthy', 'options': {},
                'functions': verdict['functions']}
    bp = verdict['breakpoint'] or {}
    return {
        'status': 'grounded',
        'options': bp.get('resume_options', {}),
        'breakpoint': bp,
        'reasons': verdict['reasons'],
    }


class RecoverySession:
    """Stateful convenience wrapper around one hardware state + history."""

    def __init__(self, source=None, *, token=None, ledger=None, matrix=None,
                 now=FIXED_NOW):
        self.hardware = as_hardware(source)
        self.token = token
        self.ledger = ledger
        self.matrix = matrix
        self.now = now

    def assess(self):
        return assess(self.hardware, matrix=self.matrix, now=self.now)

    def capture(self):
        return capture(self.hardware)

    def forward(self, package=None, **kwargs):
        return route_forward(
            self.hardware, package=package, ledger=self.ledger,
            matrix=self.matrix, now=self.now, **kwargs
        )

    def downgrade(self, token=None, **kwargs):
        return route_downgrade(
            self.hardware, token=token if token is not None else self.token,
            ledger=self.ledger, matrix=self.matrix, now=self.now, **kwargs
        )

    def power_cycle(self):
        self.hardware = self.hardware.power_cycle()
        return self


# ---------------------------------------------------------------------------
# Append-only announcement / conclusion history + difference explanation
# ---------------------------------------------------------------------------

def old_upgrade_announcement():
    return seal_document({
        'id': 'ANN-20261120-V2',
        'kind': 'upgrade_announcement',
        'issued_at': '2026-11-20T09:00:00',
        'target': 'v2',
        'title': '关于行驶控制器与作业控制器 v2 固件同步升级的公告',
        'body_zh': (
            'v2 升级包适用于 ECU1/ECU2 对应板型；两块控制器必须完成同版本升级、'
            '校准 K2 转换提交并通过逐板启动自检。未列入厂商兼容矩阵的混版组合'
            '一律停机，不得仅凭任一单板版本判为可出车。降级须持有厂商就本机'
            '签发的恢复令牌。'
        ),
    })


def old_wrong_conclusion(machine_id=DEFAULT_MACHINE_ID):
    """The retained provisional conclusion produced by the old any-board rule."""
    return {
        'id': 'C-20261123-OLD',
        'kind': 'roadworthy_conclusion',
        'issued_at': '2026-11-23T18:30:00',
        'machine_id': machine_id,
        'rule': 'any_controller_at_target',
        'ready': True,
        'can_dispatch': True,
        'roadworthy': True,
        'grounded': False,
        'controller_versions': {ECU1: 'v2', ECU2: 'v1'},
        'basis': ['ECU1 reported version v2'],
        'compatibility_matrix_checked': False,
        'calibration_commit_checked': False,
        'boot_readback_checked': False,
        'note_zh': '旧规则见任一板达到目标版本即返回真；该结论已被新证明取代但保留备查。',
    }


class HistoryLedger:
    """Append-only history.  Existing records are never mutated or removed."""

    def __init__(self, doc=None):
        self.doc = doc if doc is not None else {
            'announcements': [], 'conclusions': [],
        }
        self.doc.setdefault('announcements', [])
        self.doc.setdefault('conclusions', [])

    @classmethod
    def from_fixture(cls, fixture):
        history = fixture.setdefault('history', {
            'announcements': [], 'conclusions': [],
        })
        return cls(history)

    @property
    def announcements(self):
        return self.doc['announcements']

    @property
    def conclusions(self):
        return self.doc['conclusions']

    def add_announcement(self, record, *, seal_it=True):
        record = seal_document(record) if seal_it and 'signature' not in record \
            else dict(record)
        self.announcements.append(record)
        return record

    def seed_defaults(self, machine_id=DEFAULT_MACHINE_ID):
        if not any(a.get('id') == 'ANN-20261120-V2' for a in self.announcements):
            self.announcements.append(old_upgrade_announcement())
        if not any(c.get('id') == 'C-20261123-OLD' for c in self.conclusions):
            self.conclusions.append(old_wrong_conclusion(machine_id))
        return self

    def add_conclusion(self, verdict_or_record, *, conclusion_id=None,
                       route=None, issued_at=FIXED_NOW, summary_zh=None,
                       supersedes='C-20261123-OLD'):
        if 'functions' in verdict_or_record and 'checks' in verdict_or_record:
            verdict = verdict_or_record
            record = {
                'id': conclusion_id or f'C-NEW-{len(self.conclusions) + 1:03d}',
                'kind': 'roadworthy_conclusion',
                'issued_at': issued_at,
                'machine_id': verdict.get('machine_id'),
                'route': route,
                'rule': 'signed_matrix+boot_readback+calibration_commit',
                'ready': verdict['ready'],
                'can_dispatch': verdict['can_dispatch'],
                'roadworthy': verdict['roadworthy'],
                'grounded': verdict['grounded'],
                'controller_versions': verdict['controller_versions'],
                'matrix_entry_id': verdict.get('matrix_entry_id'),
                'functions': verdict['functions'],
                'eligible_task_types': verdict['scheduling']['eligible_task_types'],
                'amplitude_control_available':
                    verdict['scheduling']['amplitude_control_available'],
                'checks': verdict['checks'],
                'reasons': verdict['reasons'],
                'supersedes': supersedes,
                'summary_zh': summary_zh,
            }
        else:
            record = dict(verdict_or_record)
            if conclusion_id:
                record['id'] = conclusion_id
        self.conclusions.append(record)
        return record

    def get(self, record_id):
        for bucket in (self.announcements, self.conclusions):
            for record in bucket:
                if record.get('id') == record_id:
                    return record
        return None

    def difference(self, old_id, new_id):
        return difference_report(self.get(old_id), self.get(new_id))


def difference_report(old, new):
    """Explain why an old announcement/conclusion and a new verdict differ."""
    old = old or {}
    new = new or {}
    old_ready = bool(old.get('can_dispatch', old.get('ready', old.get('roadworthy'))))
    new_ready = bool(new.get('can_dispatch', new.get('ready', new.get('roadworthy'))))
    old_versions = old.get('controller_versions') or old.get('versions') or {}
    new_versions = new.get('controller_versions') or new.get('versions') or {}
    old_functions = old.get('functions')
    new_functions = new.get('functions')

    changes = []
    if old.get('rule') == 'any_controller_at_target' or old_ready and not new_ready:
        changes.append(
            'old rule accepted any single controller at target; new rule '
            'requires the exact controller combination in the vendor-signed '
            'compatibility matrix'
        )
    if old_versions and new_versions and old_versions != new_versions:
        changes.append(
            f'reported combination changed from {old_versions} to {new_versions}'
        )
    if not new_ready and new.get('reasons'):
        changes.append('new verdict grounds the machine due to: '
                       + ', '.join(new['reasons']))
    if new_ready and new_functions is not None:
        changes.append(f'new verdict certifies functions {new_functions}')
    if old.get('calibration_commit_checked') is False:
        changes.append(
            'old conclusion never checked the K1->K2 calibration commit '
            'boundary or dual-bank boot readback'
        )
    if old_ready != new_ready:
        changes.append(
            f'dispatch conclusion changed: old={old_ready}, new={new_ready}'
        )

    summary_zh = (
        f"旧记录 {old.get('id')} 的可出车结论为 {old_ready}，"
        f"新记录 {new.get('id')} 为 {new_ready}。"
        "差异来自：单板版本不得推断整机；必须同时满足签名兼容矩阵、"
        "字节签名/板型/校准模式三匹配、双分区启动回读与自检、"
        "校准提交边界及本机恢复令牌。"
    )
    return {
        'old_id': old.get('id'),
        'new_id': new.get('id'),
        'old_dispatchable': old_ready,
        'new_dispatchable': new_ready,
        'dispatch_changed': old_ready != new_ready,
        'versions_old': old_versions,
        'versions_new': new_versions,
        'functions_old': old_functions,
        'functions_new': new_functions,
        'changes': changes,
        'explanation': changes,
        'summary_zh': summary_zh,
    }


explain_difference = difference_report


def compare_recoveries(result_a, result_b):
    """Show how two legal recoveries from one breakpoint diverge."""
    va = result_a.get('assessment', result_a)
    vb = result_b.get('assessment', result_b)
    return {
        'routes': [result_a.get('route'), result_b.get('route')],
        'both_roadworthy': bool(va.get('roadworthy') and vb.get('roadworthy')),
        'functions_by_route': {
            result_a.get('route'): va.get('functions'),
            result_b.get('route'): vb.get('functions'),
        },
        'scheduling_by_route': {
            result_a.get('route'): va.get('scheduling'),
            result_b.get('route'): vb.get('scheduling'),
        },
        'difference': sorted(
            set(vb.get('functions', [])) - set(va.get('functions', []))
        ),
        'summary_zh': (
            '同一断点下两条合法路线均可出车，但 v1 降级路线仅能基础耕作；'
            'v2 前向路线额外提供变幅控制及相应调度能力。'
        ),
    }


# ---------------------------------------------------------------------------
# Whole fixed fixture document
# ---------------------------------------------------------------------------

def build_fixture(machine_id=DEFAULT_MACHINE_ID):
    """Assemble fixtures/firmware-mixed.json content with all signatures."""
    state = breakpoint_state(machine_id)
    k1 = default_k1(machine_id)
    events = [
        {'seq': 1, 'controller_id': ECU1, 'version': 'v1',
         'boot_verified': True},
        {'seq': 2, 'controller_id': ECU2, 'version': 'v1',
         'boot_verified': True},
        {'seq': 3, 'controller_id': ECU1, 'version': 'v2',
         'boot_verified': True, 'active_partition': 'B'},
    ]
    token = issue_recovery_token(
        machine_id=machine_id,
        transaction_id=state['upgrade_job']['transaction_id'],
    )
    history = {
        'announcements': [old_upgrade_announcement()],
        'conclusions': [old_wrong_conclusion(machine_id)],
    }
    readback_snapshot = capture(state)
    packages = build_default_packages(machine_id)
    return {
        'schema': SCHEMA_VERSION,
        'fixture': 'firmware-mixed',
        'machine_id': machine_id,
        'machine': machine_id,
        'target': 'v2',
        'scenario': 'power_loss_ECU1_v2_booted_ECU2_v1_K2_converting',
        'breakpoint_description_zh': (
            'ECU1 已在 B 分区成功启动 v2，A 保留 v1；ECU2 仍在 A 分区运行 v1；'
            'K2 校准页已写入但未提交，K1 仍为已提交页；升级作业未写完成标记。'
        ),
        # Minimal seed-compatible projection.
        'events': events,
        'versions': report_versions(events),
        # Rich proof material.
        'hardware': state,
        'readback': readback_snapshot,
        'capture': readback_snapshot,
        'partitions_readback': readback_snapshot['partitions'],
        'calibration': state['calibration'],
        'matrix': DEFAULT_MATRIX,
        'compatibility_matrix': DEFAULT_MATRIX,
        'packages': packages,
        'firmware_packages': packages,
        'recovery_token': token,
        'downgrade_token': token,
        'vendor_token': token,
        'history': history,
        'announcements': history['announcements'],
        'conclusions': history['conclusions'],
        'scope': SCOPE,
    }


build_mixed_fixture = build_fixture
mixed_fixture = build_fixture

FIXTURES_DIR = Path(__file__).resolve().parents[1] / 'fixtures'


def load_fixture(name='firmware-mixed.json'):
    """Load a JSON fixture from the repository ``fixtures`` directory."""
    path = Path(name)
    if not path.exists():
        path = FIXTURES_DIR / name
    return json.loads(path.read_text(encoding='utf-8'))


class CompatibilityMatrix:
    """Object wrapper around a vendor-signed matrix document."""

    def __init__(self, doc=None):
        self.doc = doc or DEFAULT_MATRIX

    @classmethod
    def default(cls):
        return cls(DEFAULT_MATRIX)

    @classmethod
    def signed(cls, entries, matrix_id='CM-CUSTOM', key=VENDOR_KEY):
        return cls(seal_document(
            {'matrix_id': matrix_id, 'entries': entries}, key
        ))

    def verify(self, key=VENDOR_KEY):
        return verify_matrix(self.doc, key)

    def entry_for(self, versions_map):
        return lookup_matrix(versions_map, self.doc)

    def compatible(self, versions_map):
        return compatible(versions_map, self.doc)

    def functions_for(self, versions_map):
        entry = self.entry_for(versions_map)
        return list((entry or {}).get('functions', []))


# Session-style alias.
FirmwareRecovery = RecoverySession
Recovery = RecoverySession


# A class-shaped alias for callers that expect a capture service object.
class FirmwareCapture:
    """Namespace/façade over the module-level proof functions."""

    capture = staticmethod(capture)
    read = staticmethod(capture)
    readback = staticmethod(capture)
    read_partitions = staticmethod(read_partitions)
    assess = staticmethod(assess)
    recovery_proof = staticmethod(recovery_proof)
    plan_recovery = staticmethod(plan_recovery)
    verify_package = staticmethod(verify_package)
    verify_matrix = staticmethod(verify_matrix)
    convert_calibration = staticmethod(convert_calibration)
    issue_recovery_token = staticmethod(issue_recovery_token)
    verify_recovery_token = staticmethod(verify_recovery_token)
    route_forward = staticmethod(route_forward)
    route_downgrade = staticmethod(route_downgrade)
    build_fixture = staticmethod(build_fixture)


# Additional naming aliases for alternative call styles.
verify_firmware_triple = verify_package
verify_package_triple = verify_package
create_recovery_token = issue_recovery_token
issue_token = issue_recovery_token
roadworthy_verdict = assess
get_verdict = assess
decide = assess
recovery_plan = plan_recovery
apply_recovery = recover
firmware_state = capture
collect = capture



def _main():
    """Print a compact offline proof of the breakpoint and both recoveries."""
    packages = build_default_packages()
    token = issue_recovery_token(machine_id=DEFAULT_MACHINE_ID)
    hold = assess(breakpoint_state())
    down = route_downgrade(breakpoint_state(), token=token)
    forward = route_forward(breakpoint_state(), packages=packages)
    proof = {
        'breakpoint': {
            'status': hold['status'],
            'can_dispatch': hold['can_dispatch'],
            'functions': hold['functions'],
            'reasons': hold['reasons'],
        },
        'downgrade_v1': {
            'ok': down['ok'],
            'can_dispatch': down['can_dispatch'],
            'functions': down['assessment']['functions'],
            'scheduling': down['assessment']['scheduling'],
        },
        'forward_v2': {
            'ok': forward['ok'],
            'can_dispatch': forward['can_dispatch'],
            'functions': forward['assessment']['functions'],
            'scheduling': forward['assessment']['scheduling'],
        },
        'scope': SCOPE,
    }
    print(json.dumps(proof, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    _main()
