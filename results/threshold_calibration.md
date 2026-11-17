# 阈值标定

> 口径：在自建场景库（5 段/5 类）上测得；数据来源：合成场景库（mock，非真实采集）。
> 网格：dwell [6.0, 8.0, 10.0, 150.0, 180.0, 200.0]、fall [0.6, 0.7, 0.8]、door [0.5, 0.6, 0.7]、estop [0.7, 0.8, 0.9]，共 162 组。

## 1. 正常场景误报（第一道过滤）

- 满足「normal 段 trapped 帧数 == 0」的候选：162 / 162
- 选择顺序固定为：先过滤零误报，再在剩余集内按 trapped recall → F1 排序（REQ-FUNC-050）。

## 2. 选定阈值（召回）

- dwell_threshold = 180.0
- fall_thresh = 0.7
- door_thresh = 0.6
- estop_thresh = 0.8
- trapped recall = 1.000000，precision = 1.000000，f1 = 1.000000

## 3. 说明

- 若选定值与 `model/config.py` 当前默认一致，则记录为「维持初值」。
- 被淘汰候选的完整数字保存在 `results/threshold_grid.csv`，可逐行追溯。
