"""L1 数据契约 v1.0 —— 锁死区。

本文件是全队唯一的结构化数据定义来源（A -> B -> C），内容原样对齐
《00_协作总览与接口契约》第 6 节，不得自行增删字段。

规则：
1. 字段名、字段顺序、字段类型、语义均不得单方面修改；
2. 任何变更必须先更新交付包中的 L1 契约文档，并在群内同步 A/C；
3. 变更后必须在同一次提交里同步更新 tests/test_schema_contract.py 的快照。
"""

from dataclasses import dataclass
from typing import List

CONTRACT_VERSION = "L1-1.0"

# 以下常量是 L1 契约中"输入形状 / 取值域"的机器可读形式，
# 供 A/B/C 在入口处做校验，不属于可自由调整的业务参数。
IMU_STEPS = 200          # 时间步
IMU_HZ = 20              # 采样率，200 步 / 20Hz = 10 秒窗口
IMU_CHANNELS = 3         # 固定 [ax, ay, az]
IMU_WINDOW_SEC = 10.0    # IMUEvent.window_sec 固定值
PERSON_COUNT_MAX = 8     # person_count 封顶，数不清填 8

EVENT_TYPES = ("normal", "estop", "trapped", "pry", "faint")

# urgency -> 允许的 event_type（L1 6.4）。trapped 最低为 2，urgency=1 只给 estop。
URGENCY_EVENT_MAP = {
    0: ("normal",),
    1: ("estop",),
    2: ("trapped", "pry"),
    3: ("faint",),
}

URGENCY_MEANING = {
    0: "正常",
    1: "关注",
    2: "告警",
    3: "紧急",
}


@dataclass
class VisionOut:
    """A -> B 的视觉感知结果（每帧一条）。"""

    ts: int                   # 毫秒时间戳
    person_count: int         # 人数 0-8（封顶，数不清就填 8）
    fall_prob: float          # 倒地概率 0-1
    door_approach: float      # 贴门程度 0-1（腕部靠近门缝，非动作识别）
    crowded: float            # 拥挤度 0-1
    keypoints_ok: bool        # 关键点是否可信；False 时 B 必须降级


@dataclass
class IMUEvent:
    """B 内部的 IMU 事件检测结果（每窗口一条）。"""

    ts: int                   # 窗口结束的毫秒时间戳
    estop: float              # 急停概率 0-1
    vibration: float          # 异常振动概率 0-1
    idle: float               # 静止概率 0-1
    window_sec: float         # 窗口长度，固定 10.0


@dataclass
class Decision:
    """B -> C 的最终判决（C 直接消费，不再加工）。"""

    ts: int                   # 毫秒时间戳（与 RDS BIGINT 对齐）
    device_id: str            # 例 "ELEV-DEMO-001"
    trapped: bool
    urgency: int              # 0 正常 / 1 关注 / 2 告警 / 3 紧急
    location: str             # 楼层字符串，例 "3F"；估不出填 "?"
    people_count: int
    event_type: str           # normal | estop | trapped | pry | faint
    confidence: float         # 0-1 整体置信度
    reason: List[str]         # 人类可读的判定理由，只列触发项且带具体数值