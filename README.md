# 农机跨地块作业结算采证台

该离线命令行工具把经营权和作业委托、机具与机手预约、定位轨迹与质量复核分别记账。每次导入保留源文件哈希与原文，重新计算全部决定，并保存实体状态和历史裁定。经营权撤销使结算退回补作，故障后轨迹不计费，故障前经独立复核的面积仍可结算。

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m fieldmachine demo --db demo.sqlite
python -m fieldmachine audit --db demo.sqlite
python -m unittest discover -s tests -v
```

单独导入命令：`field-import`、`machine-import`、`track-import`，均需 `--file` 与 `--db`。`report` 展示裁定、实体状态、业务记录及原始文件。六份 `fixtures` 按正常三账、反例三账顺序演示。地块支持 `field_polygon` 顶点与矩形回退，轨迹用整数网格单元和多边形内单元裁切；这明确限定了当前面积算法的精度。

## 共享机具章程表决（独立业务）

`members.py`、`ballot_capture.py`、`motions.py` 与 `fixtures/motion-proxy.json` 构成独立的章程表决子系统，主结果只有“章程修订是否生效”（pass / reject / pending），不改变农机产权过户、报价、工时或调度。四条硬约束闭环：

1. **股权截止日**：投票权取自 `members.shares(events, cutoff)`，截止日后生效的迟到股权转让与 `draft` 草稿不得回溯改写旧截止日；名册改判（如 A 的 60 股会前已转出）会联动翻转赞成多数或转为待核。
2. **不可双投 + 代理链可追**：同一委托人的本人票与受托票竞争时默认隔离待核，绝不按收件先后选边；代理授权按议案版本限定范围、核验时窗与撤销回执（`resolve_proxy` 遍历代理图并防环）。
3. **隐私公开与密封审计**：公开报告只含议案总权重、有效/无效/待核票的张数与权重及结果，不含会员标识、票意或盲标签；审计包用 `sha256` 盲标签逐封验证每份密封票只计一次。
4. **议案版本隔离**：旧版票不迁移到修订版（计 `old_version` 且零权重），新版结果按版本另存；`ResolutionBook` 对同一版本的重复公告抛出 `ImmutableResolutionError`，已公告决议不可覆写。

固定争议（A 亲笔赞成票与代理人 P 代投反对票同日入箱、两份转让草稿、封箱前 v1→v2 修订）在无补充证据时公告 `pending`，A 的 60 股从赞成/反对双方隔离。取得亲笔票封存见证与代理撤销回执则 60 股赞成有效、章程通过；取得有效代理授权与亲笔票作废声明则 60 股反对有效、章程否决；两路证据冲突或证据不足一律待核，不得猜票。验证：

```bash
python -B -m unittest discover -s tests -v
```

