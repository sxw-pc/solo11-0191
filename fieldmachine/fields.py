"""Land rights, mandate state, and geometric boundary evidence."""

def latest(events):
    out={}
    for e in events:
        key=e['mandate_id']
        if key not in out or (e['version'],e['id'])>(out[key]['version'],out[key]['id']):out[key]=e
    return out

def decide(events):
    out={}
    for mandate,e in latest(events).items():
        rect=e['field_rect'];geometry=len(rect)==4 and rect[0]<rect[2] and rect[1]<rect[3]
        right=e['right_from']<=e['work_start'] and e['right_until']>=e['work_end']
        conflict=any(x['mandate_id']!=mandate and x['field_id']==e['field_id'] and x['owner']!=e['owner'] and x['right_from']<=e['work_end'] and x['right_until']>=e['work_start'] for x in latest(events).values())
        canceled=e.get('canceled_at') is not None and e['canceled_at']<=e['work_start']
        valid=geometry and right and not conflict and not canceled
        out[mandate]={'event':e,'valid':valid,'status':'authorized' if valid else 'canceled' if canceled else 'conflict' if conflict else 'ineligible','geometry':geometry,'right':right,'conflict':conflict,'canceled':canceled,'field_area':(rect[2]-rect[0])*(rect[3]-rect[1]) if geometry else 0}
    return out
