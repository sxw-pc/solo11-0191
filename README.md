# 农机跨地块作业结算采证台

该离线命令行工具把经营权和作业委托、机具与机手预约、定位轨迹与质量复核分别记账。每次导入保留源文件哈希与原文，重新计算全部决定，并保存实体状态和历史裁定。经营权撤销使结算退回补作，故障后轨迹不计费，故障前经独立复核的面积仍可结算。

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m fieldmachine demo --db demo.sqlite
python -m fieldmachine audit --db demo.sqlite
python -m unittest discover -s tests -v
```

单独导入命令：`field-import`、`machine-import`、`track-import`，均需 `--file` 与 `--db`。`report` 展示裁定、实体状态、业务记录及原始文件。六份 `fixtures` 按正常三账、反例三账顺序演示。地块支持 `field_polygon` 顶点与矩形回退，轨迹用整数网格单元和多边形内单元裁切；这明确限定了当前面积算法的精度。

## 收获产量归属（独立主营账）

`harvest_capture` 把割台、流量、GPS 三路独立时钟对齐主时钟后，按割台开关窗口和实测吞吐滞后带，把延迟到达流量传感器的谷物反推回切割瞬间的地块与网格。该账只回答收获产量归属，不裁定地块压实可作业区、油量去向、机手工资或报价，四条硬约束各自落库：

- **动态滞后**：高/低吞吐对应不同滞后带，流量时间戳的 GPS 地块不直接当生长地；切割窗跨田界的边界脉冲保留 `lo–hi` 候选区间，不强行选边。
- **轨迹不重计**：割台关闭与地头转弯不产生质量，重复经过同一网格只计一次覆盖。
- **车斗与过磅守恒**：跨地块不清仓时残粮按装卸状态带入下一段；独立过磅单只做总质量守恒校验，禁止凭总量按比例硬分到田。
- **旧图留痕**：`yield_map.MapBook` 发布的图冻结不可变，迟到校准只重算受影响地块/网格并另出新版，旧图原样回读。

`fixtures/yield-boundary.json` 中 F1 在 09:00（主时 540）结束、F2 随即开始，40kg 边界脉冲无校准时两地块均为 100–140kg 且不排序；补冲洗试验则 F1=140/F2=100、先复核 F2，补旁路称重与清仓响应则 F1=100/F2=140、先复核 F1，全程过磅 240kg 不变。`yield_stream.reclassify` 按脉冲身份转移同一批 kg（如 40kg 从传感器标注 F2 移给割台 F1），`yield_map.kilograms` 随即反映相同转移。

