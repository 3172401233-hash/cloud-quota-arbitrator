"""配额仲裁核心逻辑。

仲裁顺序：
1. 校验容量可行性（容量必须不小于全部最小保障之和）。
2. 共享池 = 容量 - 最小保障之和，按 max-min 公平分配给仍有需求的租户，
   且不超过其最大配额剩余空间。
3. 租户内部按 (优先级降序, task_id) 顺序授权，放不下的进入队列。
4. 抢占：排队的高优先级任务可抢占严格更低优先级的任务，
   但被抢占租户不得低于其最小保障，且抢占以整个任务为原子单位。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import EPS, Cluster, Resource, Task, Tenant


class InfeasibleError(ValueError):
    """集群容量不足以覆盖全部租户的最小保障。"""


@dataclass(frozen=True)
class Grant:
    task_id: str
    tenant: str
    resource: Resource
    reason: str


@dataclass(frozen=True)
class Queued:
    task_id: str
    tenant: str
    demand: Resource
    reason: str


@dataclass(frozen=True)
class Preemption:
    task_id: str
    tenant: str
    freed: Resource
    reason: str


@dataclass(frozen=True)
class Release:
    """缩容时需要释放的、当前正在运行的任务。"""

    task_id: str
    tenant: str
    freed: Resource
    reason: str


@dataclass
class Plan:
    grants: list[Grant] = field(default_factory=list)
    queued: list[Queued] = field(default_factory=list)
    preemptions: list[Preemption] = field(default_factory=list)
    releases: list[Release] = field(default_factory=list)
    tenant_usage: dict[str, Resource] = field(default_factory=dict)
    explanations: list[str] = field(default_factory=list)


def _water_fill(pool: float, needs: dict[str, float]) -> dict[str, float]:
    """max-min 公平分配：每轮给未饱和租户均分剩余池，饱和者退出。"""
    alloc = {k: 0.0 for k in needs}
    active = {k for k, need in needs.items() if need > EPS}
    remaining = pool
    while remaining > EPS and active:
        share = remaining / len(active)
        saturated = [
            k for k in sorted(active) if needs[k] - alloc[k] <= share + EPS
        ]
        if not saturated:
            for k in active:
                alloc[k] += share
            remaining = 0.0
        else:
            for k in saturated:
                give = needs[k] - alloc[k]
                alloc[k] += give
                remaining -= give
                active.discard(k)
    return alloc


def _fair_share(
    pool: Resource,
    demand_beyond: dict[str, Resource],
    headroom: dict[str, Resource],
) -> dict[str, Resource]:
    """对共享池按 CPU / 内存两个维度分别做 max-min 公平分配。"""
    cpu_need = {
        t: min(demand_beyond[t].cpu, headroom[t].cpu) for t in demand_beyond
    }
    mem_need = {
        t: min(demand_beyond[t].memory, headroom[t].memory) for t in demand_beyond
    }
    cpu_alloc = _water_fill(pool.cpu, cpu_need)
    mem_alloc = _water_fill(pool.memory, mem_need)
    return {t: Resource(cpu_alloc[t], mem_alloc[t]) for t in demand_beyond}


def arbitrate(
    cluster: Cluster, tenants: list[Tenant], tasks: list[Task]
) -> Plan:
    tenant_map = {t.name: t for t in tenants}
    if len(tenant_map) != len(tenants):
        raise ValueError("存在重名租户")
    for task in tasks:
        if task.tenant not in tenant_map:
            raise ValueError(f"任务 {task.task_id} 引用了未知租户 {task.tenant}")

    total_min = Resource()
    for t in tenants:
        total_min = total_min + t.min_guarantee
    if not total_min.fits(cluster.capacity):
        raise InfeasibleError(
            f"集群容量 {cluster.capacity} 不足以覆盖最小保障之和 {total_min}，"
            "缩容已触及最小保障底线，无法继续释放"
        )

    plan = Plan()
    pool = cluster.capacity - total_min
    plan.explanations.append(
        f"共享池 = 容量({cluster.capacity.cpu}cpu/{cluster.capacity.memory}GB)"
        f" - 最小保障之和({total_min.cpu}cpu/{total_min.memory}GB)"
        f" = {pool.cpu}cpu/{pool.memory}GB"
    )

    demand: dict[str, Resource] = {t.name: Resource() for t in tenants}
    for task in tasks:
        demand[task.tenant] = demand[task.tenant] + task.demand

    demand_beyond = {
        t.name: (demand[t.name] - t.min_guarantee).clamp_zero() for t in tenants
    }
    headroom = {
        t.name: (t.max_quota - t.min_guarantee).clamp_zero() for t in tenants
    }
    shared = _fair_share(pool, demand_beyond, headroom)
    budget = {
        t.name: (t.min_guarantee + shared[t.name]).component_min(t.max_quota)
        for t in tenants
    }
    for t in tenants:
        plan.explanations.append(
            f"租户 {t.name}：最小保障 {t.min_guarantee.cpu}cpu/"
            f"{t.min_guarantee.memory}GB，分得共享池 {shared[t.name].cpu:.4g}cpu/"
            f"{shared[t.name].memory:.4g}GB，预算 {budget[t.name].cpu:.4g}cpu/"
            f"{budget[t.name].memory:.4g}GB（上限 {t.max_quota.cpu}cpu/"
            f"{t.max_quota.memory}GB）"
        )

    granted: dict[str, list[Task]] = {t.name: [] for t in tenants}
    remaining = dict(budget)
    waiting: list[Task] = []
    queue_reason: dict[str, str] = {}

    by_tenant: dict[str, list[Task]] = {t.name: [] for t in tenants}
    for task in tasks:
        by_tenant[task.tenant].append(task)
    for name in by_tenant:
        by_tenant[name].sort(key=lambda k: (-k.priority, k.task_id))

    for t in tenants:
        for task in by_tenant[t.name]:
            if task.demand.fits(remaining[t.name]):
                granted[t.name].append(task)
                remaining[t.name] = remaining[t.name] - task.demand
                plan.grants.append(
                    Grant(
                        task.task_id,
                        t.name,
                        task.demand,
                        f"优先级 {task.priority}，在租户 {t.name} 预算内授权",
                    )
                )
            else:
                waiting.append(task)
                queue_reason[task.task_id] = (
                    f"租户 {t.name} 预算剩余不足"
                    f"（剩 {remaining[t.name].cpu:.4g}cpu/"
                    f"{remaining[t.name].memory:.4g}GB，"
                    f"需 {task.demand.cpu}cpu/{task.demand.memory}GB）"
                )

    # 抢占：按优先级从高到低处理排队任务。
    waiting.sort(key=lambda k: (-k.priority, k.task_id))
    for task in waiting:
        tenant = tenant_map[task.tenant]
        used = Resource()
        for g in granted[task.tenant]:
            used = used + g.demand
        if not (used + task.demand).fits(tenant.max_quota):
            queue_reason[task.task_id] = (
                f"授权后将突破租户 {task.tenant} 最大配额 "
                f"{tenant.max_quota.cpu}cpu/{tenant.max_quota.memory}GB，保持排队"
            )
            continue

        deficit = (task.demand - remaining[task.tenant]).clamp_zero()
        victims: list[Task] = []
        # 模拟抢占：只动严格更低优先级、可抢占的任务，且不得击穿最小保障。
        candidates = sorted(
            (
                g
                for name in granted
                for g in granted[name]
                if g.preemptible and g.priority < task.priority
            ),
            key=lambda k: (k.priority, k.task_id),
        )
        sim_granted = {name: list(granted[name]) for name in granted}
        ok = True
        for cand in candidates:
            if deficit.cpu <= EPS and deficit.memory <= EPS:
                break
            vt = tenant_map[cand.tenant]
            vt_used = Resource()
            for g in sim_granted[cand.tenant]:
                vt_used = vt_used + g.demand
            floor = vt.min_guarantee.component_min(demand[cand.tenant])
            after = vt_used - cand.demand
            if not floor.fits(after):
                continue  # 抢占它会击穿最小保障，跳过
            sim_granted[cand.tenant].remove(cand)
            victims.append(cand)
            deficit = (deficit - cand.demand).clamp_zero()
        if deficit.cpu > EPS or deficit.memory > EPS:
            ok = False

        if ok and victims:
            victim_ids = {v.task_id for v in victims}
            plan.grants = [g for g in plan.grants if g.task_id not in victim_ids]
            for v in victims:
                granted[v.tenant].remove(v)
                # 释放的资源统一转给等待任务所在租户（同租户抢占等价于
                # 归还本租户预算，跨租户抢占等价于从共享池回收再分配）。
                remaining[task.tenant] = remaining[task.tenant] + v.demand
                plan.preemptions.append(
                    Preemption(
                        v.task_id,
                        v.tenant,
                        v.demand,
                        f"整体释放（优先级 {v.priority} < {task.priority}），"
                        f"为高优先级任务 {task.task_id} 腾出资源；"
                        f"其租户 {v.tenant} 仍保有最小保障",
                    )
                )
            granted[task.tenant].append(task)
            remaining[task.tenant] = remaining[task.tenant] - task.demand
            plan.grants.append(
                Grant(
                    task.task_id,
                    task.tenant,
                    task.demand,
                    f"优先级 {task.priority}，通过抢占 {len(victims)} 个"
                    "低优先级任务获得资源",
                )
            )
        elif ok:
            granted[task.tenant].append(task)
            remaining[task.tenant] = remaining[task.tenant] - task.demand
            plan.grants.append(
                Grant(
                    task.task_id,
                    task.tenant,
                    task.demand,
                    f"优先级 {task.priority}，共享池回收后授权",
                )
            )
        else:
            queue_reason[task.task_id] = (
                "共享池已耗尽，且不存在足够可抢占的更低优先级任务"
                "（受最小保障与不可抢占标记约束）"
            )

    granted_ids = {g.task_id for ts in granted.values() for g in ts}
    preempted_ids = {p.task_id for p in plan.preemptions}
    for task in tasks:
        if task.task_id in granted_ids or task.task_id in preempted_ids:
            continue
        if task.running:
            plan.releases.append(
                Release(
                    task.task_id,
                    task.tenant,
                    task.demand,
                    "容量收缩后预算不足，按优先级从低到高释放；"
                    "其租户最小保障未被突破",
                )
            )
        else:
            plan.queued.append(
                Queued(
                    task.task_id,
                    task.tenant,
                    task.demand,
                    queue_reason.get(task.task_id, "资源不足，排队等待"),
                )
            )

    for t in tenants:
        used = Resource()
        for g in granted[t.name]:
            used = used + g.demand
        plan.tenant_usage[t.name] = used.rounded()
        plan.explanations.append(
            f"租户 {t.name} 最终用量 {used.cpu:.4g}cpu/{used.memory:.4g}GB"
            f"（保障 {t.min_guarantee.cpu}cpu/{t.min_guarantee.memory}GB，"
            f"上限 {t.max_quota.cpu}cpu/{t.max_quota.memory}GB）"
        )
    plan.grants.sort(key=lambda g: g.task_id)
    plan.queued.sort(key=lambda q: q.task_id)
    plan.preemptions.sort(key=lambda p: p.task_id)
    plan.releases.sort(key=lambda r: r.task_id)
    return plan
