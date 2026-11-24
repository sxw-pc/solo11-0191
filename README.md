# 农机跨地块作业结算采证台

该离线命令行工具把经营权和作业委托、机具与机手预约、定位轨迹与质量复核分别记账。每次导入保留源文件哈希与原文，重新计算全部决定，并保存实体状态和历史裁定。经营权撤销使结算退回补作，故障后轨迹不计费，故障前经独立复核的面积仍可结算。

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m fieldmachine demo --db demo.sqlite
python -m fieldmachine audit --db demo.sqlite
python -m unittest discover -s tests -v
```

单独导入命令：`field-import`、`machine-import`、`track-import`，均需 `--file` 与 `--db`。`report` 展示裁定、实体状态、业务记录及原始文件。六份 `fixtures` 按正常三账、反例三账顺序演示。地块支持 `field_polygon` 顶点与矩形回退，轨迹用整数网格单元和多边形内单元裁切；这明确限定了当前面积算法的精度。

## 机具产权转移（独立于作业结算）

`title_capture`、`asset_titles`、`asset_transfer` 三模块只决定机具产权归属与交付权，不触碰作业面积、行驶调拨、燃油或采购报价。

- 身份共同锚定：铭牌、发动机号、登记原件三者合一；维修换牌只登记别名，同发动机号配异登记原件进隔离区，同一机具不得被转让两次。
- 处分权：卖方须为现登记所有人且无在效融资抵押/司法冻结；抵押解除前合同挂 `blocked`。
- 受理不等于完成：`transfer_receipt` 只推进申请阶段，所有权仅在带申请令牌的登记终局（`completed`）时转移；断点（申请已发出、本地无回执）禁止断言对世产权，钥匙请求一律 `hold`。
- 同机唯一与通知留痕：一台机具只允许一笔终局过户，迟到的第二笔完成登记被拒并留痕；已发买方提机通知不可静默覆盖，迟到撤销生成指名 `correction` 并要求受影响合同重裁。
- `fixtures/title-conflict.json` 演示 CoopA 同日双卖（S1→CoopB、S2→CoopC）的断点与两种登记终局；`tests/test_title_transfer.py` 覆盖换牌、抵押、双合同、断点恢复、两种终局及旧通知回读。
