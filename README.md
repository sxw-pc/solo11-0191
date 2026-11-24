# 农机跨地块作业结算采证台

该离线命令行工具把经营权和作业委托、机具与机手预约、定位轨迹与质量复核分别记账。每次导入保留源文件哈希与原文，重新计算全部决定，并保存实体状态和历史裁定。经营权撤销使结算退回补作，故障后轨迹不计费，故障前经独立复核的面积仍可结算。

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m fieldmachine demo --db demo.sqlite
python -m fieldmachine audit --db demo.sqlite
python -m unittest discover -s tests -v
```

单独导入命令：`field-import`、`machine-import`、`track-import`，均需 `--file` 与 `--db`。`report` 展示裁定、实体状态、业务记录及原始文件。六份 `fixtures` 按正常三账、反例三账顺序演示。地块支持 `field_polygon` 顶点与矩形回退，轨迹用整数网格单元和多边形内单元裁切；这明确限定了当前面积算法的精度。

## 机手工资时段账

`fieldmachine/operator_shifts.py` 把机手驾驶卡、车载终端开机/运行记录与双方签署、见证员核验的交接单归一化后，按机具切分到原子分钟归属；`fieldmachine/operator_pay.py` 的 `payable_minutes` 只汇总最终判定为驾驶应付的分钟，`disputed_minutes`/`frozen_minutes` 给出冻结争议分钟，教练陪同与路途搭乘单列为 `coaching_minutes`/`ride_minutes`。无双方签收加见证核验的交接边界不能改变归属，上传顺序无关；同一内容卡片重传或改名不重计，`card_id`+`version`/`replaces` 形成可追替换链。`fieldmachine/operator_capture.py` 的 `OperatorCapture`（别名 `WageLedger` 等）额外负责工资批次：`pay_batch` 一经发放即锁定，`apply_late_evidence` 只按分钟单元差异生成限定机具与时段的调整单，旧批次可 `get_batch`/`load` 回读，`verify_replay` 从原始卡片复算结论。

`fixtures/operator-handoff.json` 固定同一组冲突卡片：driver-1 两段共 240 分钟、driver-2 同机具 570–630 驾驶卡、车载终端只证明机具工作。无有效交接时冻结 570–630 共 60 分钟（driver-1 应付 180）；第 570 分钟双边签署并见证的交接单判归 driver-2（driver-1 180、driver-2 60）；第 630 分钟交接单加 570–630 教练陪同签认则 driver-1 仍应付 240、driver-2 的 60 分钟走陪同合同。该账独立于地块面积、购机报价与到机燃油模块。

```bash
python -B -m unittest discover -s tests -v
```
