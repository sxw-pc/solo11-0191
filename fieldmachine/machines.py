"""Machine width, inspection, qualified shift, and fault obligations."""

def latest(events):
    out={}
    for e in events:
        key=e['mandate_id']
        if key not in out or (e['version'],e['id'])>(out[key]['version'],out[key]['id']):out[key]=e
    return out

def decide(events,fields):
    out={}
    all_latest=latest(events)
    for mandate,e in all_latest.items():
        field=fields.get(mandate)
        width=bool(field and e['width_m']<=field['event']['lane_width_m'])
        inspected=bool(field and e['inspected_until']>=field['event']['work_end'])
        primary_shift=bool(field and e['operator_cert_until']>=field['event']['work_end'] and e['shift_start']<=field['event']['work_start'] and e['shift_end']>=field['event']['work_end'])
        own_end=min(e['slot_end'],e.get('fault_at') or e['slot_end'])
        overlapping=[x for x in all_latest.values() if x['mandate_id']!=mandate and x['slot_start']<own_end and min(x['slot_end'],x.get('fault_at') or x['slot_end'])>e['slot_start']]
        machine_conflict=any(x['machine_id']==e['machine_id'] for x in overlapping)
        operator_conflict=any(e.get('operator_id') and x.get('operator_id')==e['operator_id'] for x in overlapping)
        backup=e.get('backup_operator_id')
        backup_shift=bool(backup and field and e.get('backup_cert_until','')>=field['event']['work_end'] and e.get('backup_shift_start','~')<=field['event']['work_start'] and e.get('backup_shift_end','')>=field['event']['work_end'] and not any(x.get('operator_id')==backup for x in overlapping))
        substituted=bool((not primary_shift or operator_conflict) and backup_shift and not machine_conflict)
        shift=bool((primary_shift and not operator_conflict) or substituted)
        conflict=bool(machine_conflict or (operator_conflict and not substituted))
        fault=bool(e.get('fault_at') and e['slot_start']<=e['fault_at']<=e['slot_end'])
        ready=bool(field and field['valid'] and width and inspected and shift and not conflict and not fault)
        out[mandate]={'event':e,'ready':ready,'status':'fault' if fault else 'reserved' if ready else 'frozen','width':width,'inspected':inspected,'shift':shift,'conflict':conflict,'fault':fault,'released_from':e.get('fault_at') if fault else None,'substituted':substituted,'operator':backup if substituted else e.get('operator_id')}
    return out
