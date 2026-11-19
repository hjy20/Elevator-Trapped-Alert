"""合成轿厢场景：在没有实拍素材时，先造出"格式完全合规"的演示数据。

诚实声明（同样会写进 docs/DATA_DICT.md）
------------------------------------------
这里渲染的是**程序化合成的轿厢示意图**，不是实拍视频；但除了"像素是不是相机拍的"之外，
一切都按真机流水线的口径来：

* 画面：1280x720 / 25fps / H.264；构图、逆光、噪点、透视与现场摄像头近似；
* 关键点：由同一套 2D 骨骼在世界坐标里驱动，**画面里画的人体和 sidecar 里的
  17 个关键点严格同源**，所以视觉层看到的几何关系是真实的一致关系；
* 抖动：给关键点叠加高斯抖动 + 随机丢点，模拟 YOLO-Pose 在逆光/遮挡下的真实表现，
  时序滤波才有东西可滤。

换成实拍素材只需要：把手机拍的视频丢进 `data/raw/`，跑 `prepare_real_video.py`，
推理时会自动走 YOLO-Pose 后端（见 README）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

# COCO-17 顺序
NOSE, L_EYE, R_EYE, L_EAR, R_EAR = 0, 1, 2, 3, 4
L_SHO, R_SHO, L_ELB, R_ELB, L_WRI, R_WRI = 5, 6, 7, 8, 9, 10
L_HIP, R_HIP, L_KNE, R_KNE, L_ANK, R_ANK = 11, 12, 13, 14, 15, 16

__all__ = [
    "POSE_LIBRARY",
    "TrackKey",
    "Track",
    "SceneGeometry",
    "CabinRenderer",
    "project_keypoints",
]


def _pose(**kwargs: Tuple[float, float]) -> np.ndarray:
    """用名字构造 (17,2) 骨骼（单位米，y 向上，原点在脚下地面）。"""
    names = [
        "nose", "l_eye", "r_eye", "l_ear", "r_ear",
        "l_sho", "r_sho", "l_elb", "r_elb", "l_wri", "r_wri",
        "l_hip", "r_hip", "l_kne", "r_kne", "l_ank", "r_ank",
    ]
    out = np.zeros((17, 2), dtype=np.float64)
    for index, name in enumerate(names):
        if name in kwargs:
            out[index] = kwargs[name]
    return out


# ---------------------------------------------------------------------------
# 姿态库（单位：米；y 向上，x 向画面右）
# ---------------------------------------------------------------------------

_STAND_BASE = _pose(
    nose=(0.0, 1.60), l_eye=(0.030, 1.635), r_eye=(-0.030, 1.635),
    l_ear=(0.065, 1.615), r_ear=(-0.065, 1.615),
    l_sho=(0.175, 1.38), r_sho=(-0.175, 1.38),
    l_elb=(0.200, 1.12), r_elb=(-0.200, 1.12),
    l_wri=(0.215, 0.86), r_wri=(-0.215, 0.86),
    l_hip=(0.095, 0.92), r_hip=(-0.095, 0.92),
    l_kne=(0.100, 0.51), r_kne=(-0.100, 0.51),
    l_ank=(0.105, 0.07), r_ank=(-0.105, 0.07),
)

POSE_LIBRARY: Dict[str, np.ndarray] = {
    "stand": _STAND_BASE,
    "stand_shift": _STAND_BASE
    + np.array([[0.02, 0.0]] * 17, dtype=np.float64),
    "walk_a": _STAND_BASE.copy(),
    "walk_b": _STAND_BASE.copy(),
    # 蹲下捡东西 / 系鞋带（躯干仍接近竖直 —— 用来验证"不误报倒地"）
    "crouch": _pose(
        nose=(0.02, 1.12), l_eye=(0.05, 1.15), r_eye=(-0.01, 1.15),
        l_ear=(0.09, 1.13), r_ear=(-0.05, 1.13),
        l_sho=(0.19, 0.94), r_sho=(-0.17, 0.94),
        l_elb=(0.24, 0.70), r_elb=(-0.22, 0.70),
        l_wri=(0.20, 0.50), r_wri=(-0.16, 0.48),
        l_hip=(0.10, 0.46), r_hip=(-0.10, 0.46),
        l_kne=(0.22, 0.30), r_kne=(-0.22, 0.30),
        l_ank=(0.12, 0.06), r_ank=(-0.12, 0.06),
    ),
    # 弯腰系鞋带（躯干前倾但角度 < 60°，也不该触发倒地）
    "bend": _pose(
        nose=(0.34, 1.02), l_eye=(0.34, 1.05), r_eye=(0.30, 1.05),
        l_ear=(0.38, 1.03), r_ear=(0.27, 1.03),
        l_sho=(0.16, 1.12), r_sho=(-0.02, 1.12),
        l_elb=(0.26, 0.86), r_elb=(0.06, 0.84),
        l_wri=(0.24, 0.60), r_wri=(0.04, 0.56),
        l_hip=(0.10, 0.90), r_hip=(-0.10, 0.90),
        l_kne=(0.10, 0.50), r_kne=(-0.10, 0.50),
        l_ank=(0.12, 0.06), r_ank=(-0.12, 0.06),
    ),
    # 仰面倒地（头朝画面左，躯干水平 —— 夹角 ≈ 88°）
    "lie": _pose(
        nose=(-0.72, 0.17), l_eye=(-0.70, 0.21), r_eye=(-0.70, 0.12),
        l_ear=(-0.65, 0.20), r_ear=(-0.65, 0.11),
        l_sho=(-0.50, 0.19), r_sho=(-0.50, 0.09),
        l_elb=(-0.56, 0.05), r_elb=(-0.44, 0.24),
        l_wri=(-0.72, 0.05), r_wri=(-0.34, 0.28),
        l_hip=(-0.02, 0.16), r_hip=(-0.02, 0.08),
        l_kne=(0.40, 0.17), r_kne=(0.40, 0.07),
        l_ank=(0.82, 0.11), r_ank=(0.80, 0.05),
    ),
    # 坐在地上（夹角 ~ 35°，不算倒地）
    "sit": _pose(
        nose=(0.02, 1.06), l_eye=(0.05, 1.09), r_eye=(-0.01, 1.09),
        l_ear=(0.09, 1.07), r_ear=(-0.05, 1.07),
        l_sho=(0.18, 0.86), r_sho=(-0.17, 0.86),
        l_elb=(0.22, 0.62), r_elb=(-0.21, 0.62),
        l_wri=(0.18, 0.40), r_wri=(-0.17, 0.40),
        l_hip=(0.10, 0.42), r_hip=(-0.10, 0.42),
        l_kne=(0.36, 0.44), r_kne=(-0.34, 0.44),
        l_ank=(0.70, 0.08), r_ank=(-0.68, 0.08),
    ),
    # 单手扒门（左手伸向门缝方向，腕部停在门缝高度）
    "reach_door": _pose(
        nose=(0.05, 1.58), l_eye=(0.08, 1.61), r_eye=(0.02, 1.61),
        l_ear=(0.12, 1.59), r_ear=(-0.02, 1.59),
        l_sho=(0.19, 1.37), r_sho=(-0.16, 1.37),
        l_elb=(0.30, 1.24), r_elb=(-0.12, 1.10),
        l_wri=(0.42, 1.36), r_wri=(-0.16, 0.86),
        l_hip=(0.10, 0.92), r_hip=(-0.10, 0.92),
        l_kne=(0.10, 0.50), r_kne=(-0.12, 0.50),
        l_ank=(0.11, 0.06), r_ank=(-0.13, 0.06),
    ),
    # 双手推门/拍门
    "push_door": _pose(
        nose=(0.03, 1.57), l_eye=(0.06, 1.60), r_eye=(0.00, 1.60),
        l_ear=(0.10, 1.58), r_ear=(-0.04, 1.58),
        l_sho=(0.18, 1.36), r_sho=(-0.16, 1.36),
        l_elb=(0.30, 1.22), r_elb=(0.02, 1.20),
        l_wri=(0.40, 1.18), r_wri=(0.24, 1.16),
        l_hip=(0.10, 0.92), r_hip=(-0.10, 0.92),
        l_kne=(0.12, 0.50), r_kne=(-0.12, 0.50),
        l_ank=(0.13, 0.06), r_ank=(-0.13, 0.06),
    ),
}

# 走路两步（左右腿交替），由 stand 派生
POSE_LIBRARY["walk_a"] = POSE_LIBRARY["stand"].copy()
POSE_LIBRARY["walk_a"][[L_KNE, L_ANK]] = np.array([[0.17, 0.52], [0.25, 0.07]])
POSE_LIBRARY["walk_a"][[R_KNE, R_ANK]] = np.array([[-0.14, 0.50], [-0.21, 0.06]])
POSE_LIBRARY["walk_a"][[L_ELB, L_WRI]] = np.array([[0.24, 1.13], [0.27, 0.88]])
POSE_LIBRARY["walk_a"][[R_ELB, R_WRI]] = np.array([[-0.15, 1.10], [-0.11, 0.85]])

POSE_LIBRARY["walk_b"] = POSE_LIBRARY["stand"].copy()
POSE_LIBRARY["walk_b"][[L_KNE, L_ANK]] = np.array([[0.14, 0.50], [0.21, 0.06]])
POSE_LIBRARY["walk_b"][[R_KNE, R_ANK]] = np.array([[-0.17, 0.52], [-0.25, 0.07]])
POSE_LIBRARY["walk_b"][[L_ELB, L_WRI]] = np.array([[0.15, 1.10], [0.11, 0.85]])
POSE_LIBRARY["walk_b"][[R_ELB, R_WRI]] = np.array([[-0.24, 1.13], [-0.27, 0.88]])


def smoothstep(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return t * t * (3.0 - 2.0 * t)


def blend_pose(a: np.ndarray, b: np.ndarray, t: float) -> np.ndarray:
    w = smoothstep(t)
    return (1.0 - w) * a + w * b


# ---------------------------------------------------------------------------
# 轨迹：关键帧 -> 逐帧骨骼
# ---------------------------------------------------------------------------


@dataclass
class TrackKey:
    """一个人在 t 时刻的状态快照。"""

    t: float
    pose: str = "stand"
    x: float = 0.0                      # 相对站位的水平偏移（米）
    y: float = 0.0                      # 整体上下平移（米），用来做"被抱起/滑落"
    occlude: Tuple[int, ...] = ()       # 这些关键点在这一段压低置信度
    note: str = ""


@dataclass
class Track:
    """一个人的时间线，外加外观参数（画图和 sidecar 都要用）。"""

    keys: List[TrackKey]
    depth: float = 0.9                 # 0-1，1 = 最近
    x_base: float = 0.0                # 站位（米，相对画面中线）
    palette: Tuple[str, str, str] = ("jacket", "pants", "skin")
    seed: int = 0
    label: str = "p1"

    # -- 采样 --------------------------------------------------------------
    def sample(self, t: float) -> Tuple[np.ndarray, float, float, Tuple[int, ...]]:
        if not self.keys:
            return POSE_LIBRARY["stand"].copy(), self.x_base, 0.0, ()
        if t <= self.keys[0].t:
            key = self.keys[0]
            return POSE_LIBRARY[key.pose].copy(), self.x_base + key.x, key.y, key.occlude
        if t >= self.keys[-1].t:
            key = self.keys[-1]
            return POSE_LIBRARY[key.pose].copy(), self.x_base + key.x, key.y, key.occlude

        for left, right in zip(self.keys, self.keys[1:]):
            if left.t <= t <= right.t:
                span = max(1e-6, right.t - left.t)
                ratio = (t - left.t) / span
                kpts = blend_pose(POSE_LIBRARY[left.pose], POSE_LIBRARY[right.pose], ratio)
                x = self.x_base + left.x + (right.x - left.x) * smoothstep(ratio)
                y = left.y + (right.y - left.y) * smoothstep(ratio)
                occlude = left.occlude if ratio < 0.5 else right.occlude
                return kpts, x, y, occlude
        key = self.keys[-1]
        return POSE_LIBRARY[key.pose].copy(), self.x_base + key.x, key.y, key.occlude


# ---------------------------------------------------------------------------
# 场景几何与投影
# ---------------------------------------------------------------------------


@dataclass
class SceneGeometry:
    """把世界坐标（米）投到像素的正视/弱透视相机。"""

    width: int = 1280
    height: int = 720
    floor_y: float = 700.0            # 最近处（depth=1）脚底所在像素 y
    px_per_m: float = 200.0           # depth=1 时 1 米对应多少像素
    depth_falloff: float = 560.0      # depth 每减 1，脚底上移多少像素
    origin_x: float = 640.0           # 画面中线的像素 x

    def project(self, kpts_m: np.ndarray, x_m: float, y_m: float, depth: float) -> np.ndarray:
        """(17,2) 米 -> (17,2) 像素。"""
        scale = self.px_per_m * float(depth)
        foot_y = self.floor_y - (1.0 - float(depth)) * self.depth_falloff
        out = np.empty((kpts_m.shape[0], 2), dtype=np.float64)
        out[:, 0] = self.origin_x + (kpts_m[:, 0] + x_m) * scale
        out[:, 1] = foot_y - (kpts_m[:, 1] + y_m) * scale
        return out

    def meters_in_px(self, depth: float) -> float:
        return self.px_per_m * float(depth)


def project_keypoints(
    kpts_m: np.ndarray,
    x_m: float,
    y_m: float,
    depth: float,
    geom: SceneGeometry,
    *,
    rng: Optional[np.random.Generator] = None,
    jitter_px: float = 1.2,
    occlude: Sequence[int] = (),
    occlude_conf: float = 0.08,
) -> np.ndarray:
    """世界坐标 -> (17,3) 关键点（含抖动与丢点），直接就是 sidecar 的行。"""
    px = geom.project(kpts_m, x_m, y_m, depth)
    if rng is not None and jitter_px > 0:
        px = px + rng.normal(0.0, jitter_px, size=px.shape)
    conf = np.clip(0.92 + (rng.normal(0.0, 0.04, size=(17,)) if rng is not None else 0.0), 0.45, 0.99)
    conf[[NOSE, L_SHO, R_SHO, L_HIP, R_HIP]] = np.clip(0.95 + (rng.normal(0.0, 0.02, size=5) if rng is not None else 0.0), 0.6, 0.99)
    # 逆光最容易丢的是四肢末端
    for index in (L_WRI, R_WRI, L_ANK, R_ANK, L_KNE, R_KNE):
        conf[index] = float(np.clip(conf[index] - 0.12, 0.35, 0.98))
    for index in occlude:
        conf[int(index)] = occlude_conf
    stacked = np.concatenate([px, conf.reshape(-1, 1)], axis=1).astype(np.float32)
    return stacked


def person_visible_px(kpts_px: np.ndarray, width: int, height: int, margin: float = 0.0) -> bool:
    """人是否还在画面里（用关键点均值的投影判断，与"画出来/写进 sidecar"同一口径）。

    合成场景里"走出画面"必须同时满足三件事：不再绘制、不再写 sidecar、
    不再算进逐帧真值。真实检测器看不见画外的人，这三者必须一致。
    """
    mean_x = float(kpts_px[:, 0].mean())
    mean_y = float(kpts_px[:, 1].mean())
    return (margin <= mean_x <= width - margin) and (margin <= mean_y <= height - margin)


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------

_CLOTHING = {
    "navy": (118, 78, 46),
    "grey": (120, 120, 120),
    "red": (60, 60, 170),
    "green": (90, 120, 80),
    "black": (48, 48, 52),
    "beige": (150, 168, 190),
    "denim": (140, 100, 60),
}
_PANTS = {
    "dark": (55, 55, 60),
    "denim": (130, 96, 58),
    "grey": (105, 105, 110),
}
_SKIN = (152, 176, 205)


class CabinRenderer:
    """把轿厢背景 + 若干 Track 渲染成一帧 BGR 画面。"""

    def __init__(self, geom: Optional[SceneGeometry] = None, *, seed: int = 7) -> None:
        self.geom = geom or SceneGeometry()
        self.rng = np.random.default_rng(seed)
        self._static = self._build_background()
        self._vignette = self._build_vignette()
        self._noise_cache: Optional[np.ndarray] = None

    # -- 背景 --------------------------------------------------------------
    def _build_background(self) -> np.ndarray:
        import cv2

        g = self.geom
        img = np.zeros((g.height, g.width, 3), dtype=np.uint8)

        # 后墙：不锈钢拉丝（竖直细条纹 + 上亮下暗）
        wall_bottom = int(g.floor_y - 0.36 * g.depth_falloff)
        for y in range(wall_bottom):
            v = 148 - int(58 * (y / max(1, wall_bottom))) 
            img[y, :] = (v + 6, v + 2, v - 4)
        stripe = np.zeros((wall_bottom, g.width, 3), dtype=np.int16)
        for x in range(0, g.width, 7):
            value = int(14 * math.sin(x * 0.7))
            stripe[:, x:x + 1] = value
        img[:wall_bottom] = np.clip(
            img[:wall_bottom].astype(np.int16) + stripe, 0, 255
        ).astype(np.uint8)

        # 墙面踢脚线
        cv2.rectangle(img, (0, wall_bottom - 16), (g.width, wall_bottom), (66, 66, 70), -1)
        cv2.line(img, (0, wall_bottom - 17), (g.width, wall_bottom - 17), (120, 120, 124), 1)

        # 地面：从墙根向画面下方渐亮，带透视铺板
        floor = img.copy()
        for y in range(wall_bottom, g.height):
            ratio = (y - wall_bottom) / max(1, (g.height - wall_bottom))
            v = int(58 + 46 * ratio)
            floor[y, :] = (v + 4, v + 2, v)
        img = floor
        cv2.line(img, (0, wall_bottom), (g.width, wall_bottom), (150, 150, 155), 2)

        # 地板砖缝（向消失点收拢的斜线）
        vp_x, vp_y = g.width // 2, wall_bottom - 60
        for k in range(-6, 7):
            x_bottom = int(g.width / 2 + k * 150)
            cv2.line(img, (x_bottom, g.height), (vp_x, vp_y), (96, 96, 100), 1, cv2.LINE_AA)
        for frac in (0.16, 0.30, 0.46, 0.64, 0.84):
            y = int(wall_bottom + frac * (g.height - wall_bottom))
            cv2.line(img, (0, y), (g.width, y), (92, 92, 96), 1, cv2.LINE_AA)

        # 后墙扶手
        rail_y = int(wall_bottom - 210)
        cv2.line(img, (40, rail_y), (g.width - 40, rail_y), (176, 176, 184), 7, cv2.LINE_AA)
        cv2.line(img, (40, rail_y - 3), (g.width - 40, rail_y - 3), (208, 208, 214), 2, cv2.LINE_AA)

        # 楼层显示屏（后墙左中部）
        disp = (150, 96, 300, 96)
        cv2.rectangle(img, (disp[0], disp[1]), (disp[0] + disp[2], disp[1] + disp[3]), (28, 28, 32), -1)
        cv2.rectangle(img, (disp[0], disp[1]), (disp[0] + disp[2], disp[1] + disp[3]), (96, 96, 100), 2)
        cv2.putText(img, "3F", (disp[0] + 96, disp[1] + 70), cv2.FONT_HERSHEY_SIMPLEX, 1.9, (60, 220, 250), 5, cv2.LINE_AA)
        cv2.putText(img, "^", (disp[0] + 210, disp[1] + 48), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (60, 220, 250), 3, cv2.LINE_AA)

        # 控制面板（后墙右侧靠门）
        panel = (840, 210, 70, 150)
        cv2.rectangle(img, (panel[0], panel[1]), (panel[0] + panel[2], panel[1] + panel[3]), (86, 88, 94), -1)
        cv2.rectangle(img, (panel[0], panel[1]), (panel[0] + panel[2], panel[1] + panel[3]), (150, 152, 158), 2)
        for i in range(5):
            cv2.circle(img, (panel[0] + 35, panel[1] + 26 + i * 26), 9, (70, 190, 220), -1, cv2.LINE_AA)
            cv2.circle(img, (panel[0] + 35, panel[1] + 26 + i * 26), 9, (30, 90, 110), 1, cv2.LINE_AA)

        # 天花板灯带（顶部高光 + 向下衰减）
        lamp = np.zeros_like(img)
        cv2.rectangle(lamp, (330, 0), (950, 34), (235, 240, 245), -1)
        img = cv2.addWeighted(img, 1.0, lamp, 0.55, 0)
        glow = np.zeros_like(img)
        cv2.ellipse(glow, (640, 40), (420, 210), 0, 0, 180, (58, 62, 66), -1)
        img = cv2.add(img, glow)

        return img

    def _build_vignette(self) -> np.ndarray:
        g = self.geom
        ys, xs = np.mgrid[0:g.height, 0:g.width]
        dx = (xs - g.width / 2) / (g.width / 2)
        dy = (ys - g.height / 2) / (g.height / 2)
        r = np.sqrt(dx ** 2 + dy ** 2) / math.sqrt(2.0)
        mask = np.clip(1.0 - 0.55 * r ** 2.2, 0.35, 1.0).astype(np.float32)
        return np.dstack([mask] * 3)

    # -- 门 ----------------------------------------------------------------
    def _draw_door(self, img: np.ndarray, open_ratio: float) -> None:
        import cv2

        x0, y0, x1, y1 = 940, 94, 1228, 430
        seam = 1084
        half = (seam - x0)
        # 门洞（黑）
        cv2.rectangle(img, (x0, y0), (x1, y1), (18, 18, 20), -1)
        shift = int(round(open_ratio * half * 0.94))
        left_panel = (x0 - shift, y0, seam - shift, y1)
        right_panel = (seam + shift, y0, x1 + shift * 0 + 0, y1)
        for (px0, py0, px1, py1) in (left_panel, right_panel):
            if px1 <= px0:
                continue
            cv2.rectangle(img, (px0, py0), (px1, py1), (132, 134, 140), -1)
            cv2.rectangle(img, (px0 + 4, py0 + 4), (px1 - 4, py1 - 4), (150, 152, 158), 2)
            # 门板反光
            cv2.line(img, (px0 + 10, py1 - 20), (px1 - 10, py0 + 30), (188, 190, 196), 2, cv2.LINE_AA)
        # 门框
        cv2.rectangle(img, (x0 - 10, y0 - 10), (1228 + 10, 432), (168, 170, 176), 4)

    # -- 人 ----------------------------------------------------------------
    def draw_person(self, img: np.ndarray, kpts_px: np.ndarray, palette: Tuple[str, str, str], *, fallen: bool = False) -> None:
        import cv2

        cloth = _CLOTHING.get(palette[0], _CLOTHING["navy"])
        pants = _PANTS.get(palette[1], _PANTS["dark"])
        skin = _SKIN
        pts = kpts_px[:, :2]
        scale = float(np.linalg.norm(pts[L_SHO] - pts[L_HIP]) + 1.0)

        # 地面阴影
        cx = float(np.mean([pts[L_ANK, 0], pts[R_ANK, 0], pts[L_HIP, 0], pts[R_HIP, 0]]))
        ground_y = float(max(pts[L_ANK, 1], pts[R_ANK, 1]))
        shadow_w = int(max(20, 0.30 * scale * 2.2))
        overlay = img.copy()
        cv2.ellipse(overlay, (int(cx), int(ground_y + 4)), (shadow_w, int(shadow_w * 0.30)), 0, 0, 360, (18, 18, 22), -1)
        img[:] = cv2.addWeighted(overlay, 0.42, img, 0.58, 0)

        def capsule(p1, p2, radius, colour, dark=True):
            p1 = (int(p1[0]), int(p1[1]))
            p2 = (int(p2[0]), int(p2[1]))
            if dark:
                cv2.line(img, p1, p2, tuple(int(c * 0.60) for c in colour), int(radius * 2 + 2), cv2.LINE_AA)
            cv2.line(img, p1, p2, colour, int(radius * 2), cv2.LINE_AA)
            cv2.circle(img, p1, int(radius), colour, -1, cv2.LINE_AA)
            cv2.circle(img, p2, int(radius), colour, -1, cv2.LINE_AA)

        leg_r = max(3.0, 0.052 * scale)
        arm_r = max(2.5, 0.040 * scale)
        head_r = max(5.0, 0.085 * scale)

        # 腿
        capsule(pts[L_HIP], pts[L_KNE], leg_r, pants)
        capsule(pts[L_KNE], pts[L_ANK], leg_r * 0.9, pants)
        capsule(pts[R_HIP], pts[R_KNE], leg_r, pants)
        capsule(pts[R_KNE], pts[R_ANK], leg_r * 0.9, pants)
        # 鞋
        for ankle, hip in ((L_ANK, L_HIP), (R_ANK, R_HIP)):
            fwd = 1 if pts[ankle, 0] >= pts[hip, 0] else -1
            cv2.ellipse(img, (int(pts[ankle, 0] + fwd * head_r * 0.5), int(pts[ankle, 1])),
                        (int(head_r * 0.85), int(head_r * 0.45)), 0, 0, 360, (34, 34, 38), -1, cv2.LINE_AA)
        # 躯干
        torso = np.array([pts[L_SHO], pts[R_SHO], pts[R_HIP], pts[L_HIP]], dtype=np.int32)
        cv2.fillConvexPoly(img, torso, cloth, cv2.LINE_AA)
        cv2.polylines(img, [torso], True, tuple(int(c * 0.55) for c in cloth), 2, cv2.LINE_AA)
        # 手臂
        capsule(pts[L_SHO], pts[L_ELB], arm_r, cloth)
        capsule(pts[L_ELB], pts[L_WRI], arm_r * 0.9, cloth)
        capsule(pts[R_SHO], pts[R_ELB], arm_r, cloth)
        capsule(pts[R_ELB], pts[R_WRI], arm_r * 0.9, cloth)
        # 手
        for wrist in (L_WRI, R_WRI):
            cv2.circle(img, (int(pts[wrist, 0]), int(pts[wrist, 1])), int(arm_r * 1.15), skin, -1, cv2.LINE_AA)
        # 脖子 + 头
        neck = (pts[L_SHO] + pts[R_SHO]) / 2.0
        head_c = (pts[NOSE] + np.array([0.0, head_r * 0.55]))
        capsule(neck, head_c, max(2.0, head_r * 0.35), skin)
        cv2.circle(img, (int(head_c[0]), int(head_c[1])), int(head_r), skin, -1, cv2.LINE_AA)
        # 头发（上半圈）
        cv2.ellipse(img, (int(head_c[0]), int(head_c[1])), (int(head_r), int(head_r)),
                    0, 180, 360, (42, 40, 44), int(head_r * 0.75), cv2.LINE_AA)
        if fallen:
            cv2.ellipse(img, (int(head_c[0]), int(head_c[1])), (int(head_r * 0.9), int(head_r * 0.9)),
                        0, 0, 360, (60, 60, 200), 2, cv2.LINE_AA)

    # -- 单帧 --------------------------------------------------------------
    def render(
        self,
        t: float,
        tracks: Sequence[Track],
        *,
        door_open: float = 0.0,
        shake: Tuple[float, float] = (0.0, 0.0),
        fallen_labels: Sequence[str] = (),
        clock_text: Optional[str] = None,
        add_noise: bool = True,
    ) -> Tuple[np.ndarray, List[np.ndarray], List[str]]:
        """返回 (画面, 逐人关键点(17,3), 逐人标签)。"""
        import cv2

        img = self._static.copy()
        self._draw_door(img, float(door_open))

        keypoints: List[np.ndarray] = []
        labels: List[str] = []
        # 远的人先画
        order = sorted(range(len(tracks)), key=lambda i: tracks[i].depth)
        drawn: List[Tuple[float, np.ndarray, Tuple[str, str, str], str]] = []
        for index in order:
            track = tracks[index]
            kpts_m, x_m, y_m, occlude = track.sample(t)
            rng = np.random.default_rng(track.seed * 100003 + int(round(t * 25)))
            kpts = project_keypoints(kpts_m, x_m, y_m, track.depth, self.geom, rng=rng, occlude=occlude)
            if not person_visible_px(kpts, self.geom.width, self.geom.height):
                continue          # 已经走出画面：不画、不写 sidecar、不算真值
            drawn.append((track.depth, kpts, track.palette, track.label))

        for depth, kpts, palette, label in drawn:
            self.draw_person(img, kpts, palette, fallen=label in fallen_labels)
            keypoints.append(kpts)
            labels.append(label)

        # 轻微抖动（模拟轿厢振动/急停冲击）
        sx, sy = int(round(shake[0])), int(round(shake[1]))
        if sx or sy:
            matrix = np.float32([[1, 0, sx], [0, 1, sy]])
            img[:] = cv2.warpAffine(img, matrix, (self.geom.width, self.geom.height), borderMode=cv2.BORDER_REFLECT)

        img = (img.astype(np.float32) * self._vignette).astype(np.uint8)

        if add_noise:
            noise = self.rng.normal(0.0, 3.2, size=(self.geom.height, self.geom.width, 1)).astype(np.float32)
            img = np.clip(img.astype(np.float32) + noise, 0, 255).astype(np.uint8)

        # 监控水印
        text = clock_text or "CAM-01 ELEV-3F"
        cv2.putText(img, text, (24, self.geom.height - 26), cv2.FONT_HERSHEY_SIMPLEX, 0.62, (220, 220, 224), 2, cv2.LINE_AA)
        cv2.circle(img, (self.geom.width - 46, 34), 8, (40, 40, 210), -1, cv2.LINE_AA)
        cv2.putText(img, "REC", (self.geom.width - 108, 42), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (60, 60, 220), 2, cv2.LINE_AA)
        return img, keypoints, labels