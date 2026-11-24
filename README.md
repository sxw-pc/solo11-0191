# 农机跨地块作业结算采证台

该离线命令行工具把经营权和作业委托、机具与机手预约、定位轨迹与质量复核分别记账。每次导入保留源文件哈希与原文，重新计算全部决定，并保存实体状态和历史裁定。经营权撤销使结算退回补作，故障后轨迹不计费，故障前经独立复核的面积仍可结算。

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m fieldmachine demo --db demo.sqlite
python -m fieldmachine audit --db demo.sqlite
python -m unittest discover -s tests -v
```

单独导入命令：`field-import`、`machine-import`、`track-import`，均需 `--file` 与 `--db`。`report` 展示裁定、实体状态、业务记录及原始文件。六份 `fixtures` 按正常三账、反例三账顺序演示。地块支持 `field_polygon` 顶点与矩形回退，轨迹用整数网格单元和多边形内单元裁切；这明确限定了当前面积算法的精度。

## 控制器固件混版与离线恢复证明

`fieldmachine/firmware.py` 的 `versions(events)` 仍只按 `controller_id` 保留最后上报版本；`fieldmachine/fleet_update.py` 的 `ready(events, target)` 已纠正“任一板达到目标即 ready”的错误：ECU1=v2/ECU2=v1 的混版（见 `fixtures/firmware-ok.json` 与固定断点 `fixtures/firmware-mixed.json`）必须返回假。只有两板均为目标版本、无失败启动证据时简单投影才为真。

完整离线证明在 `fieldmachine/firmware_capture.py`：固件字节 HMAC 签名、板型适配、校准数据模式三匹配；K1→K2 是生成定点量、分段幅宽与转换摘要的真实转换；双分区活动/待启动槽位从建模硬件回读；整机结论 `assess()` 只输出**当前可安全运行的功能集合与能否重新出车**，并明确排除地块结算、险情撤离与调拨路线。未落入厂商签名兼容矩阵的组合一律 grounded、功能集为空并给出恢复断点。

固定断点（ECU1 已在 B 分区启动 v2、A 保留 v1；ECU2 仍在 A 运行 v1；K2 已写入未提交、作业无完成标记）支持两条合法且断电幂等的路线：

- `route_downgrade(..., token=...)`：凭绑定本机的厂商恢复令牌，从 ECU1 保留 A 槽恢复 v1、回退未提交 K2；两板 v1/K1 可出车做基础耕作，**不可用变幅控制**。
- `route_forward(...)`：先做 K2 可验证提交（已编程则只提交，断电重试绝不重复不可逆校准写入），再刷 ECU2 v2、arm B 槽并逐板启动自检；两板 v2/K2 可出车且**可用变幅控制**及对应调度任务。

任一步失败都保持停机并返回带 `failed_step` 的断点。旧升级公告 `ANN-20261120-V2` 与旧“任一板即 ready”结论 `C-20261123-OLD` 由 `HistoryLedger` 追加保留，`difference_report()` 解释新旧差异。

```bash
python -B -m unittest discover -s tests -v   # 混版/三匹配/校准中断/两条恢复/历史公告
python -B -m fieldmachine.firmware_capture    # 打印断点与两条路线的功能/调度结论
```
