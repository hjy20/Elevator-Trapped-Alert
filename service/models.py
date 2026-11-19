"""
服务层请求模型（Pydantic v2）。

★ 字段与 common/schema.py 的 Decision 一一对应，但刻意定义在 service/ 内部：
   C 不修改共用契约区，也不依赖队友是否已建好文件。

为什么要有校验：B 的融合代码还在迭代期，宁可在这里返回 422 并指出哪个字段错了，
也不要让脏数据落库、把看板搞乱。
"""
from typing import List, Literal

from pydantic import BaseModel, Field, field_validator

EVENT_TYPES = ("normal", "estop", "trapped", "pry", "faint")


class DecisionIn(BaseModel):
    ts: int = Field(..., description="毫秒时间戳")
    device_id: str = Field(..., min_length=1, description='例 "ELEV-DEMO-001"')
    trapped: bool = False
    urgency: int = Field(0, ge=0, le=3, description="0正常/1关注/2告警/3紧急")
    location: str = Field("?", description='楼层，例 "3F"；估不出填 "?"')
    people_count: int = Field(0, ge=0, le=8, description="人数，封顶 8")
    event_type: Literal["normal", "estop", "trapped", "pry", "faint"] = "normal"
    confidence: float = Field(0.0, ge=0.0, le=1.0)
    reason: List[str] = Field(default_factory=list, description="触发的判定理由，必须可追到数值")

    @field_validator("reason")
    @classmethod
    def _clean_reason(cls, v: List[str]) -> List[str]:
        # 去掉空串和首尾空白，别让看板出现空条目
        return [str(x).strip() for x in v if str(x).strip()]

    @field_validator("location")
    @classmethod
    def _clean_location(cls, v: str) -> str:
        v = (v or "").strip()
        return v if v else "?"

    model_config = {
        "json_schema_extra": {
            "example": {
                "ts": 1763300000000,
                "device_id": "ELEV-DEMO-001",
                "trapped": True,
                "urgency": 3,
                "location": "3F",
                "people_count": 2,
                "event_type": "faint",
                "confidence": 0.91,
                "reason": ["dwell=412s > 180s", "fall_prob=0.87 > 0.70"],
            }
        }
    }
