"""Project field, machine and track evidence into independent business verdicts."""
from . import fields, machines, tracks

def business_records(e):
    kind=e['kind']
    spec={'field':('地块作业权益','mandate',e['mandate_id']),
          'machine':('机具排程与资格','reservation',e['mandate_id']),
          'track':('轨迹面积与费用','settlement',e['mandate_id'])}[kind]
    return [(spec[0],spec[1],spec[2],e)]

def calculate(events):
    by={k:[e for e in events if e['kind']==k] for k in ('field','machine','track')}
    f=fields.decide(by['field']);m=machines.decide(by['machine'],f);t=tracks.decide(by['track'],f,m)
    decisions={};states=[]
    def put(name,ok,reason,evidence):
        decisions[name]={'result':'pass' if ok else 'hold','reason':reason,'evidence':evidence}
    for mandate,x in f.items():
        e=x['event'];base=f'field:{mandate}:'
        put(base+'right',x['right'],'经营权覆盖委托作业时窗' if x['right'] else '经营权时段不足',[e['id']])
        put(base+'geometry',x['geometry'] and not x['conflict'],'地块边界有效且无重复经营冲突' if not x['conflict'] else '重叠经营权冻结',[e['id']])
        put(base+'cancellation',not x['canceled'],'委托仍生效' if not x['canceled'] else '撤销未履行义务',[e['id']])
        states.append(('地块作业权益',mandate,x['status'],e['version']))
    for mandate,x in m.items():
        e=x['event'];base=f'machine:{mandate}:'
        put(base+'width',x['width'],'幅宽适配' if x['width'] else '机具幅宽超通道',[e['id']])
        put(base+'inspection',x['inspected'],'检验覆盖全窗' if x['inspected'] else '检验过期',[e['id']])
        put(base+'shift',x['shift'] and not x['conflict'],'机手资格班次和排程有效' if not x['conflict'] else '机具重叠预约',[e['id']])
        put(base+'fault',not x['fault'],'未报故障' if not x['fault'] else '故障释放未来预约',[e['id']])
        states.append(('机具排程与资格',mandate,x['status'],e['version']))
    for mandate,x in t.items():
        e=x['event'];base=f'track:{mandate}:'
        put(base+'boundary',x['outside_area']==0,'轨迹在授权地块内' if not x['outside_area'] else '越界轨迹扣减',[e['id']])
        put(base+'repeat',x['repeated_area']==0,'无重复面积' if not x['repeated_area'] else '重叠轨迹只计一次',[e['id']])
        put(base+'quality',x['quality'],'独立质量样本达标' if x['quality'] else '质量复核触发补作',[e['id']])
        put(base+'settlement',x['payable_area']>0,f"结算 {x['payable_area']} 单元，余量 {x['remaining_area']} 单元",[e['id']])
        states.append(('轨迹面积与费用',mandate,x['status'],e['version']))
    return decisions,states
