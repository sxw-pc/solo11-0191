# 农机跨地块作业结算采证台

该离线命令行工具把经营权和作业委托、机具与机手预约、定位轨迹与质量复核分别记账。每次导入保留源文件哈希与原文，重新计算全部决定，并保存实体状态和历史裁定。经营权撤销使结算退回补作，故障后轨迹不计费，故障前经独立复核的面积仍可结算。

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m fieldmachine demo --db demo.sqlite
python -m fieldmachine audit --db demo.sqlite
python -m unittest discover -s tests -v
```

单独导入命令：`field-import`、`machine-import`、`track-import`，均需 `--file` 与 `--db`。`report` 展示裁定、实体状态、业务记录及原始文件。六份 `fixtures` 按正常三账、反例三账顺序演示。面积为整数网格单元，坐标为半开矩形；这明确限定了当前面积算法的适用范围。
