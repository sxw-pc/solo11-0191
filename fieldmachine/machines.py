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
        shift=bool(field and e['operator_cert_until']>=field['event']['work_end'] and e['shift_start']<=field['event']['work_start'] and e['shift_end']>=field['event']['work_end'])
        own_end=min(e['slot_end'],e.get('fault_at') or e['slot_end'])
        conflict=any(x['mandate_id']!=mandate and (x['machine_id']==e['machine_id'] or (x.get('operator_id') and x.get('operator_id')==e.get('operator_id'))) and x['slot_start']<own_end and min(x['slot_end'],x.get('fault_at') or x['slot_end'])>e['slot_start'] for x in all_latest.values())
        fault=bool(e.get('fault_at') and e['slot_start']<=e['fault_at']<=e['slot_end'])
        ready=bool(field and field['valid'] and width and inspected and shift and not conflict and not fault)
        out[mandate]={'event':e,'ready':ready,'status':'fault' if fault else 'reserved' if ready else 'frozen','width':width,'inspected':inspected,'shift':shift,'conflict':conflict,'fault':fault,'released_from':e.get('fault_at') if fault else None}
    return out
