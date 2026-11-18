# cloud-quota-arbitrator

Python 云资源配额仲裁库：当集群资源不足、各命名空间（租户）争抢 CPU 与内存时，
根据租户最小保障、最大配额、共享池与任务优先级，生成 **grant（授权）/ queue（排队）/
preempt（抢占）** 计划，并对每个仲裁决定给出可解释的原因。

## 快速开始

```python
from quota_arbiter import Cluster, Resource, Task, Tenant, arbitrate

cluster = Cluster(Resource(cpu=9, memory=9))
tenants = [
    Tenant("A", min_guarantee=Resource(2, 2), max_quota=Resource(8, 8)),
    Tenant("B", min_guarantee=Resource(2, 2), max_quota=Resource(8, 8)),
]
tasks = [
    Task("a1", tenant="A", priority=1, demand=Resource(2, 2), running=True),
    Task("a2", tenant="A", priority=1, demand=Resource(2, 2), running=True),
    Task("a3", tenant="A", priority=1, demand=Resource(1, 1)),
    Task("b_high", tenant="B", priority=9, demand=Resource(5, 5)),
]

plan = arbitrate(cluster, tenants, tasks)
for p in plan.preemptions:
    print(p.task_id, p.reason)   # a1 被整体抢占，为 b_high 腾出资源
for line in plan.explanations:
    print(line)                  # 每个仲裁决定的完整解释
```

运行测试：`python -m unittest`

## 仲裁流程

1. **可行性校验**：集群容量必须不小于全部租户最小保障之和，
   否则抛出 `InfeasibleError`（缩容到底线，拒绝继续释放）。
2. **共享池划分**：共享池 = 容量 − 最小保障之和，按 CPU / 内存两个维度分别做
   max-min 公平分配，且不超过各租户最大配额的剩余空间。
3. **租户内授权**：租户预算 = 最小保障 + 公平份额（ capped by 最大配额），
   任务按（优先级降序, task_id）排序依次授权，放不下的进入排队。
4. **抢占**：排队的高优先级任务可抢占**严格更低**优先级的可抢占任务，
   被抢占任务整体释放（原子，不存在部分释放），且被抢占租户不得低于其最小保障。
5. **缩容释放**：容量下降时，预算不足的运行中任务按优先级从低到高进入
   `releases` 列表，最小保障永远不被突破。

## 公平与优先级的取舍

- **租户间靠公平，任务间靠优先级**。共享池在租户之间按 max-min 公平分配，
   优先级只在租户预算内部排序、以及跨租户抢占时起作用。这避免了单个高优
   租户饿死其他租户——每个租户的最小保障是硬底线。
- **最小保障优先于一切优先级**。抢占模拟会逐个检查候选受害者：一旦释放它
   会让其租户跌破 `min(最小保障, 租户总需求)`，该任务就被跳过。因此高优任务
   只能挤占"超出保障部分"的低优资源。
- **最大配额优先于抢占**。即使优先级最高，授权后会突破本租户 `max_quota`
   的任务也只能排队（零配额租户因此永远拿不到资源）。
- **抢占是原子的**。被抢占任务要么完整保留、要么整体释放，释放量恒等于其
   全部需求，不会出现"释放一半"的中间态，简化了上层调度器的回滚逻辑。
- **确定性**。所有排序都有 task_id 兜底，相同输入必然产生相同计划，
   便于审计与重放；每个 grant / queue / preempt / release 决定都附带
   人类可读的 `reason`，`plan.explanations` 汇总了共享池划分与各租户
   最终用量的完整推理过程。

## 已知限制

- 资源维度为 CPU / 内存两维，公平分配按维度独立进行，未实现主导资源公平（DRF）。
- 抢占是单次扫描的贪心策略，不做全局最优重排。
