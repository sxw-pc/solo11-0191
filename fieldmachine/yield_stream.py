"""Collect offline harvester grain-flow pulses.

增强后仍按流量事件自带 field_id 分组；该字段是传感器所在地块的迟到标注，
不是谷物生长地。跨地块边界脉冲的改判由 harvest_capture 的动态滞后证据驱动，
reclassify 只执行证据已经成立的同一批质量转移，绝不凭空创造或销毁 kg。
"""


def pulses(events):
    """按传感器标注的 field_id 原样分组（保持既有调用方行为）。"""
    result = {}
    for event in events:
        result.setdefault(event['field_id'], []).append(event)
    return result


def reclassify(events, pulse_id, from_field, to_field, kg):
    """把同一脉冲的 kg 从传感器标注地块改判给割台收割地块。

    守恒前提：被转移脉冲必须确实登记在 from_field 下且 kg 一致；
    转移前后事件总质量不变，仅 field_id 改变。
    """
    moved, out = 0.0, []
    for event in events:
        if event.get('pulse_id') == pulse_id and event['field_id'] == from_field:
            if abs(event['kg'] - kg) > 1e-9:
                raise ValueError(
                    f'改判质量 {kg} kg 与脉冲 {pulse_id} 实测 {event["kg"]} kg 不符，守恒被破坏')
            event = {**event, 'field_id': to_field, 'reclassified_from': from_field}
            moved += kg
        out.append(event)
    if abs(moved - kg) > 1e-9:
        raise ValueError(f'脉冲 {pulse_id} 未在 {from_field} 找到 {kg} kg，不能改判')
    return out


def flow_events(pulses_master):
    """harvest_capture 主时脉冲转成 yield_stream 兼容事件列表。

    入参可以是 normalize_clocks 的主时 dict（at_min），也可以是
    PulseAttribution 冻结对象（sensor_time_master）。
    """
    def time_of(p):
        return p.get('at_min') if isinstance(p, dict) else p.sensor_time_master

    def field_of(p):
        return p.get('sensor_field_id') if isinstance(p, dict) else p.sensor_field_id

    def throughput_of(p):
        return p.get('throughput') if isinstance(p, dict) else p.throughput

    return [{'pulse_id': p['pulse_id'], 'field_id': field_of(p),
             'minute': time_of(p), 'kg': p['kg'],
             'throughput': throughput_of(p)} for p in pulses_master]


def total_kg(events):
    return sum(e['kg'] for e in events)
