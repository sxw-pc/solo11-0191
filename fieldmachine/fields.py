"""Land rights, mandate state, and geometric boundary evidence."""

def polygon(e):
    if e.get('field_polygon'):return e['field_polygon']
    a,b,c,d=e['field_rect']
    return [[a,b],[c,b],[c,d],[a,d]]

def coverage_cells(e):
    points=polygon(e)
    if len(points)<3:return set()
    if sum(points[i][0]*points[(i+1)%len(points)][1]-points[(i+1)%len(points)][0]*points[i][1] for i in range(len(points)))==0:return set()
    def inside(px,py):
        included=False
        for i in range(len(points)):
            x1,y1=points[i-1];x2,y2=points[i]
            if (y1>py)!=(y2>py) and px<(x2-x1)*(py-y1)/(y2-y1)+x1:included=not included
        return included
    return {(x,y) for x in range(min(p[0] for p in points),max(p[0] for p in points)) for y in range(min(p[1] for p in points),max(p[1] for p in points)) if inside(x+0.5,y+0.5)}

def latest(events):
    out={}
    for e in events:
        key=e['mandate_id']
        if key not in out or (e['version'],e['id'])>(out[key]['version'],out[key]['id']):out[key]=e
    return out

def decide(events):
    out={}
    for mandate,e in latest(events).items():
        rect=e['field_rect'];covered=coverage_cells(e);geometry=len(rect)==4 and rect[0]<rect[2] and rect[1]<rect[3] and bool(covered)
        right=e['right_from']<=e['work_start'] and e['right_until']>=e['work_end']
        conflict=any(x['mandate_id']!=mandate and x['owner']!=e['owner'] and bool(covered & coverage_cells(x)) and x['right_from']<=e['work_end'] and x['right_until']>=e['work_start'] for x in latest(events).values())
        canceled=e.get('canceled_at') is not None and e['canceled_at']<=e['work_start']
        valid=geometry and right and not conflict and not canceled
        out[mandate]={'event':e,'valid':valid,'status':'authorized' if valid else 'canceled' if canceled else 'conflict' if conflict else 'ineligible','geometry':geometry,'right':right,'conflict':conflict,'canceled':canceled,'field_area':len(covered),'covered_cells':covered}
    return out
