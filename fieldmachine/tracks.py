"""Area union, boundary clipping, quality samples, and settlement."""
from . import fields

def latest(events):
    out={}
    for e in events:
        key=e['mandate_id']
        if key not in out or (e['version'],e['id'])>(out[key]['version'],out[key]['id']):out[key]=e
    return out

def cells(rect):
    return {(x,y) for x in range(rect[0],rect[2]) for y in range(rect[1],rect[3])}

def decide(events,fields,machines):
    out={}
    for mandate,e in latest(events).items():
        field=fields.get(mandate);machine=machines.get(mandate)
        allowed=field['covered_cells'] if field and field['geometry'] else set()
        raw=[];valid=[];repeated=set();outside=set();after_fault=set()
        for segment in e['segments']:
            region=cells(segment['rect']);raw.append(region)
            if machine and machine['fault'] and segment['at']>=machine['event']['fault_at']:
                after_fault|=region;continue
            if field and not (field['event']['work_start']<=segment['at']<=field['event']['work_end']):continue
            outside|=region-allowed
            valid.append(region&allowed)
        seen=set()
        for region in valid:repeated|=seen&region;seen|=region
        samples=e['quality_samples'];quality=bool(samples and len({x['witness'] for x in samples})>=2 and all(x['depth_cm']>=e['minimum_depth_cm'] for x in samples))
        machine_eligible=machine and machine['width'] and machine['inspected'] and machine['shift'] and not machine['conflict']
        payable=len(seen) if field and field['valid'] and machine_eligible and quality else 0
        requested=min(field['event']['requested_area'],len(allowed)) if field else 0
        out[mandate]={'event':e,'status':'payable' if payable else 'rework','payable_area':payable,'amount_cents':payable*e['price_per_cell'],'remaining_area':max(0,requested-payable),'repeated_area':len(repeated),'outside_area':len(outside),'fault_area':len(after_fault),'quality':quality,'measured_area':len(seen)}
    return out
