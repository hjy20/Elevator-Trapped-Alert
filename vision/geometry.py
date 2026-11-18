"""关键点几何工具：人数聚类、倒地角度、髋高、贴门距离。

设计原则：**这一层只做纯几何，不做时间判断**。
时序（3 秒投票、1 秒持续）全部放在 ``vision/temporal.py``，
这样公式可以单独画图、单独讲，答辩时能一条条拆开讲清楚。

COCO-17 关键点顺序（YOLO-Pose 的输出顺序，不许改）：
    0 鼻 1 左眼 2 右眼 3 左耳 4 右耳
    5 左肩 6 右肩 7 左肘 8 右肘 9 左腕 10 右腕
    11 左髋 12 右髋 13 左膝 14 右膝 15 左踝 16 右踝
每个点 3 个数：(x, y, conf)，x/y 为像素坐标。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Sequence, Tuple

import numpy as np

from .config import DoorRoi, FallConfig

__all__ = [
    "NOSE", "L_EYE", "R_EYE", "L_EAR", "R_EAR",
    "L_SHO", "R_SHO", "L_ELB", "R_ELB", "L_WRI", "R_WRI",
    "L_HIP", "R_HIP", "L_KNE", "R_KNE", "L_ANK", "R_ANK",
    "FACE_IDX", "SHOULDER_IDX", "WRIST_IDX",
    "SKELETON_EDGES",
    "Person",
    "persons_from_arrays",
    "visible_mask",
    "point",
    "mean_conf",
    "torso_angle_deg",
    "body_length_px",
    "hip_ratio",
    "fall_features",
    "door_features",
    "cluster_points",
    "count_heads",
    "count_shoulders",
]

# ---------------------------------------------------------------------------
# 关键点索引
# ---------------------------------------------------------------------------

NOSE, L_EYE, R_EYE, L_EAR, R_EAR = 0, 1, 2, 3, 4
L_SHO, R_SHO = 5, 6
L_ELB, R_ELB = 7, 8
L_WRI, R_WRI = 9, 10
L_HIP, R_HIP = 11, 12
L_KNE, R_KNE = 13, 14
L_ANK, R_ANK = 15, 16

FACE_IDX: Tuple[int, ...] = (NOSE, L_EYE, R_EYE, L_EAR, R_EAR)
SHOULDER_IDX: Tuple[int, ...] = (L_SHO, R_SHO)
HIP_IDX: Tuple[int, ...] = (L_HIP, R_HIP)
WRIST_IDX: Tuple[int, ...] = (L_WRI, R_WRI)

#: 可视化用的骨架连线（COCO 17）
SKELETON_EDGES: Tuple[Tuple[int, int], ...] = (
    (L_EYE, R_EYE), (NOSE, L_EYE), (NOSE, R_EYE),
    (L_EAR, L_EYE), (R_EAR, R_EYE),
    (L_SHO, R_SHO), (L_SHO, L_ELB), (L_ELB, L_WRI),
    (R_SHO, R_ELB), (R_ELB, R_WRI),
    (L_SHO, L_HIP), (R_SHO, R_HIP), (L_HIP, R_HIP),
    (L_HIP, L_KNE), (L_KNE, L_ANK), (R_HIP, R_KNE), (R_KNE, R_ANK),
    (NOSE, L_SHO), (NOSE, R_SHO),
)


# ---------------------------------------------------------------------------
# 单人
# ---------------------------------------------------------------------------


@dataclass
class Person:
    """一个人的 17 个关键点 + 包围框。"""

    keypoints: np.ndarray                     # (17, 3) float32: x, y, conf
    score: float = 1.0                        # 检测置信度
    bbox: Optional[np.ndarray] = None         # (4,) x1,y1,x2,y2；None 时由关键点推
    track_id: Optional[int] = None            # 预留：接 tracker 后可用

    def __post_init__(self) -> None:
        self.keypoints = np.asarray(self.keypoints, dtype=np.float32).reshape(17, 3)
        if self.bbox is None:
            self.bbox = bbox_from_keypoints(self.keypoints)
        else:
            self.bbox = np.asarray(self.bbox, dtype=np.float32).reshape(4)

    # -- 便捷属性 ----------------------------------------------------------
    @property
    def x1(self) -> float:
        return float(self.bbox[0])

    @property
    def y1(self) -> float:
        return float(self.bbox[1])

    @property
    def x2(self) -> float:
        return float(self.bbox[2])

    @property
    def y2(self) -> float:
        return float(self.bbox[3])

    @property
    def width(self) -> float:
        return max(1.0, self.x2 - self.x1)

    @property
    def height(self) -> float:
        return max(1.0, self.y2 - self.y1)

    @property
    def area(self) -> float:
        return self.width * self.height

    @property
    def center(self) -> Tuple[float, float]:
        return ((self.x1 + self.x2) / 2.0, (self.y1 + self.y2) / 2.0)


def bbox_from_keypoints(keypoints: np.ndarray, conf_thr: float = 0.15) -> np.ndarray:
    """按可信关键点外扩 8% 得到包围框（用于赶不上检测框时兜底）。"""
    kpts = np.asarray(keypoints, dtype=np.float32).reshape(17, 3)
    mask = kpts[:, 2] >= conf_thr
    if not mask.any():
        return np.zeros(4, dtype=np.float32)
    pts = kpts[mask, :2]
    x1, y1 = pts.min(axis=0)
    x2, y2 = pts.max(axis=0)
    w, h = max(4.0, x2 - x1), max(4.0, y2 - y1)
    pad_x, pad_y = 0.08 * w, 0.04 * h
    return np.array([x1 - pad_x, y1 - pad_y, x2 + pad_x, y2 + pad_y], dtype=np.float32)


def persons_from_arrays(
    keypoints: Sequence[np.ndarray], scores: Optional[Sequence[float]] = None
) -> List[Person]:
    people: List[Person] = []
    for index, kpts in enumerate(keypoints):
        score = float(scores[index]) if scores is not None else 1.0
        people.append(Person(keypoints=kpts, score=score))
    return people


# ---------------------------------------------------------------------------
# 单点/单向量工具
# ---------------------------------------------------------------------------


def visible_mask(person: Person, conf_thr: float) -> np.ndarray:
    return person.keypoints[:, 2] >= conf_thr


def point(person: Person, index: int, conf_thr: float = 0.0) -> Optional[np.ndarray]:
    """可信则返回 (x, y)，否则 None。"""
    kpt = person.keypoints[index]
    if kpt[2] < conf_thr:
        return None
    return np.array([kpt[0], kpt[1]], dtype=np.float32)


def group_midpoint(
    person: Person, indices: Iterable[int], conf_thr: float
) -> Optional[np.ndarray]:
    """一组关键点里可信点的均值；一个都不可信返回 None。"""
    pts = [point(person, i, conf_thr) for i in indices]
    pts = [p for p in pts if p is not None]
    if not pts:
        return None
    return np.mean(np.stack(pts), axis=0)


def mean_conf(person: Person, indices: Optional[Iterable[int]] = None) -> float:
    kpts = person.keypoints
    if indices is not None:
        idx = list(indices)
        return float(kpts[idx, 2].mean()) if idx else 0.0
    return float(kpts[:, 2].mean())


# ---------------------------------------------------------------------------
# 倒地几何
# ---------------------------------------------------------------------------


def torso_angle_deg(person: Person, conf_thr: float = 0.30) -> Optional[float]:
    """躯干（髋中点 -> 肩中点）与画面竖直方向的夹角，单位度。

    站立 ≈ 0-20°，坐着 ≈ 30-50°，躺倒 ≈ 70-90°。
    肩或髋不可信时返回 None（宁可不算，也不瞎算）。
    """
    shoulder = group_midpoint(person, SHOULDER_IDX, conf_thr)
    hip = group_midpoint(person, HIP_IDX, conf_thr)
    if shoulder is None or hip is None:
        return None
    vec = shoulder - hip                      # 图像坐标：y 向下
    length = float(np.linalg.norm(vec))
    if length < 1e-3:
        return None
    # 与竖直向上 (0, -1) 的夹角
    cos = float(-vec[1] / length)
    cos = max(-1.0, min(1.0, cos))
    return math.degrees(math.acos(cos))


def body_length_px(person: Person) -> float:
    """身体尺度的稳健估计：站立取框高，躺倒取框宽，因此取两者较大值。"""
    return max(person.width, person.height)


def hip_ratio(person: Person, conf_thr: float = 0.30, ground_y: Optional[float] = None) -> Optional[float]:
    """髋部高度 / 站立时髋部高度（站立 ≈ 1.0）。

    站立时髋部离地约 0.45 倍身高；蹲下会明显变小，躺倒接近 0。
    某帧框高被裁切时用 frame 底边兜底（ground_y）。
    """
    hip = group_midpoint(person, HIP_IDX, conf_thr)
    if hip is None:
        return None
    length = body_length_px(person)
    if length < 20.0:
        return None
    ground = float(person.y2) if ground_y is None else float(ground_y)
    hip_height = max(0.0, ground - float(hip[1]))
    reference = 0.45 * length
    if reference <= 1e-3:
        return None
    return hip_height / reference


@dataclass
class FallFeatures:
    """一帧里对"倒地"的全部证据，方便写进 reason 和文档。"""

    suspect: bool = False
    angle_deg: Optional[float] = None
    hip_ratio: Optional[float] = None
    head_below_hip: bool = False
    visible_kpts: int = 0
    evaluable: bool = False
    crouch_like: bool = False
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "suspect": self.suspect,
            "angle_deg": None if self.angle_deg is None else round(self.angle_deg, 1),
            "hip_ratio": None if self.hip_ratio is None else round(self.hip_ratio, 2),
            "head_below_hip": self.head_below_hip,
            "visible_kpts": self.visible_kpts,
            "evaluable": self.evaluable,
            "crouch_like": self.crouch_like,
        }


def fall_features(person: Person, cfg: FallConfig) -> FallFeatures:
    """单帧倒地几何判定（不含时序）。

    两条独立证据任一成立即"疑似倒地"：
      A. 躯干与竖直方向夹角 >= angle_deg（默认 60°）
      B. 头部中心 y 明显低于髋部 y（整个人是倒着的）

    防误报：髋高比值 < hip_drop_ratio 且躯干仍然接近竖直 → 判为"蹲/弯腰"，
    只作为弱证据（不单独触发），并把 crouch_like 标记出去。
    """
    features = FallFeatures()
    mask = visible_mask(person, cfg.kpt_conf)
    features.visible_kpts = int(mask.sum())
    if features.visible_kpts < cfg.min_visible_kpts:
        features.notes.append(f"visible_kpts={features.visible_kpts} < {cfg.min_visible_kpts}")
        return features

    features.angle_deg = torso_angle_deg(person, cfg.kpt_conf)
    features.hip_ratio = hip_ratio(person, cfg.kpt_conf)

    head = group_midpoint(person, FACE_IDX, cfg.kpt_conf)
    hip = group_midpoint(person, HIP_IDX, cfg.kpt_conf)
    if head is not None and hip is not None:
        features.head_below_hip = bool(head[1] > hip[1] + 0.02 * body_length_px(person))

    if features.angle_deg is None and not features.head_below_hip:
        features.notes.append("肩/髋不可信，无法判定")
        return features

    features.evaluable = True
    angle_hit = features.angle_deg is not None and features.angle_deg >= cfg.angle_deg
    head_hit = cfg.head_below_hip and features.head_below_hip

    if features.hip_ratio is not None and features.hip_ratio < cfg.hip_drop_ratio:
        if not angle_hit and not head_hit:
            features.crouch_like = True
            features.notes.append("髋部偏低但躯干竖直：判为蹲下/弯腰")
        elif angle_hit and features.angle_deg is not None and features.angle_deg < 75.0:
            # 蹲着又有点歪：不直接判倒地，等时序层投票
            features.crouch_like = True
            features.notes.append("髋部偏低且躯干略歪：疑似蹲下，弱证据")

    features.suspect = bool((angle_hit or head_hit) and not features.crouch_like)
    return features


# ---------------------------------------------------------------------------
# 贴门几何
# ---------------------------------------------------------------------------


def _distance_to_seam(point_xy: np.ndarray, roi: DoorRoi) -> float:
    return abs(float(point_xy[0]) - float(roi.seam_x))


def door_features(
    person: Person, roi: DoorRoi, cfg_wrist_conf: float = 0.30, near_ratio: float = 0.28
) -> Tuple[float, List[np.ndarray]]:
    """腕部是否贴住门缝。

    返回 (几何得分 0-1, 命中的腕部像素点列表)。

    得分映射：手在门框 ROI 内、且距门缝 <= near_ratio * 门宽 → 1.0，
    距离再远就线性衰减到 0；手不在 ROI 内直接 0。
    """
    x, y, w, h = roi.roi
    hit_points: List[np.ndarray] = []
    if w <= 1 or h <= 1:
        return 0.0, hit_points

    near_limit = max(1.0, near_ratio * float(w))
    # 衰减边界必须离"贴着门缝"很近：站在门边自然垂手不该被算成扒门。
    # 0.22 个门宽 ≈ 40 cm，超过这个距离几何分直接为 0。
    far_limit = max(near_limit * 1.8, 0.22 * float(w))
    best = 0.0
    for index in WRIST_IDX:
        wrist = point(person, index, cfg_wrist_conf)
        if wrist is None:
            continue
        wx, wy = float(wrist[0]), float(wrist[1])
        if not (x <= wx <= x + w and y <= wy <= y + h):
            continue
        dist = _distance_to_seam(wrist, roi)
        if dist <= near_limit:
            score = 1.0
        elif dist >= far_limit:
            score = 0.0
        else:
            score = 1.0 - (dist - near_limit) / (far_limit - near_limit)
        if score > 0.35:
            hit_points.append(wrist)
        best = max(best, score)
    return float(best), hit_points


# ---------------------------------------------------------------------------
# 人数：关键点聚类
# ---------------------------------------------------------------------------


def cluster_points(points: np.ndarray, radius: float) -> List[List[int]]:
    """贪心聚类（并查集），radius 为像素距离阈值。

    为什么不用 bbox 计数：轿厢拥挤时框会重叠、NMS 会把两个人合并成一个框，
    而头和肩膀的关键点位置基本不会重合，聚类更稳。
    """
    pts = np.asarray(points, dtype=np.float32).reshape(-1, 2)
    n = pts.shape[0]
    if n == 0:
        return []
    parent = list(range(n))

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    radius_sq = float(radius) ** 2
    for i in range(n):
        for j in range(i + 1, n):
            dx = pts[i, 0] - pts[j, 0]
            dy = pts[i, 1] - pts[j, 1]
            if dx * dx + dy * dy <= radius_sq:
                union(i, j)

    groups: dict[int, List[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return list(groups.values())


def _reference_radius(people: Sequence[Person]) -> float:
    """聚类半径随人身上下文自适应：约 0.6 个肩宽。"""
    widths = []
    for person in people:
        left = point(person, L_SHO, 0.3)
        right = point(person, R_SHO, 0.3)
        if left is not None and right is not None:
            widths.append(float(np.linalg.norm(left - right)))
    if widths:
        return max(18.0, 0.9 * float(np.median(widths)))
    heights = [person.height for person in people if person.height > 20]
    if heights:
        return max(18.0, 0.18 * float(np.median(heights)))
    return 40.0


def count_heads(people: Sequence[Person], conf_thr: float = 0.30) -> int:
    """按"头部点聚类"数人数：每人只贡献一个头部中心，再做跨人的聚类合并。"""
    centers: List[np.ndarray] = []
    for person in people:
        head = group_midpoint(person, FACE_IDX, conf_thr)
        if head is not None:
            centers.append(head)
    if not centers:
        return 0
    clusters = cluster_points(np.stack(centers), _reference_radius(people))
    return len(clusters)


def count_shoulders(people: Sequence[Person], conf_thr: float = 0.30) -> int:
    """肩膀中点聚类，作为头部计数的交叉校验（低头/背对镜头时的补充证据）。"""
    centers: List[np.ndarray] = []
    for person in people:
        shoulder = group_midpoint(person, SHOULDER_IDX, conf_thr)
        if shoulder is not None:
            centers.append(shoulder)
    if not centers:
        return 0
    clusters = cluster_points(np.stack(centers), _reference_radius(people))
    return len(clusters)