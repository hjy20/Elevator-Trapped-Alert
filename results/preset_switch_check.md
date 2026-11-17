# 双阈值切换检查

- 生产 dwell_threshold = 180.0s
- 演示 dwell_threshold = 8.0s
- 同段 22 帧数据生产告警数 = 0
- 同段 22 帧数据演示告警数 = 6
- 代码逻辑零改动：算法模块无 demo_threshold 分支（AST 校验通过）
