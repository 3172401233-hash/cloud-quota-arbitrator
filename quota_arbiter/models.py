"""仲裁器输入数据模型。"""
from __future__ import annotations

from dataclasses import dataclass

EPS = 1e-9


@dataclass(frozen=True)
class Resource:
    """二维资源向量：CPU（核）与内存（GB）。"""

    cpu: float = 0.0
    memory: float = 0.0

    def __add__(self, other: "Resource") -> "Resource":
        return Resource(self.cpu + other.cpu, self.memory + other.memory)

    def __sub__(self, other: "Resource") -> "Resource":
        return Resource(self.cpu - other.cpu, self.memory - other.memory)

    def fits(self, other: "Resource") -> bool:
        """self 的每个维度都不超过 other。"""
        return self.cpu <= other.cpu + EPS and self.memory <= other.memory + EPS

    def positive(self) -> bool:
        return self.cpu > EPS or self.memory > EPS

    def clamp_min(self, floor: "Resource") -> "Resource":
        return Resource(max(self.cpu, floor.cpu), max(self.memory, floor.memory))

    def clamp_zero(self) -> "Resource":
        return Resource(max(self.cpu, 0.0), max(self.memory, 0.0))

    def component_min(self, other: "Resource") -> "Resource":
        return Resource(min(self.cpu, other.cpu), min(self.memory, other.memory))

    def rounded(self, digits: int = 6) -> "Resource":
        return Resource(round(self.cpu, digits), round(self.memory, digits))


@dataclass(frozen=True)
class Tenant:
    """租户配额：最小保障与最大配额。"""

    name: str
    min_guarantee: Resource
    max_quota: Resource


@dataclass(frozen=True)
class Task:
    """任务：属于某租户，带优先级与资源需求。

    priority 数值越大优先级越高；preemptible=False 的任务不可被抢占。
    running 表示当前已占用资源（用于缩容时生成 release 计划）。
    """

    task_id: str
    tenant: str
    priority: int
    demand: Resource
    running: bool = False
    preemptible: bool = True


@dataclass(frozen=True)
class Cluster:
    """集群总容量。共享池 = capacity - sum(租户最小保障)。"""

    capacity: Resource
