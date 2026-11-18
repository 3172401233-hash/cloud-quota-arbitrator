"""配额仲裁器单元测试。

覆盖：共享池耗尽、高优任务到达触发抢占、租户超配额、
节点容量变化（缩容）、零配额，以及确定性与抢占原子性。
"""
import unittest

from quota_arbiter import (
    Cluster,
    InfeasibleError,
    Resource,
    Task,
    Tenant,
    arbitrate,
)


def res(cpu, mem):
    return Resource(cpu, mem)


def grant_ids(plan):
    return {g.task_id for g in plan.grants}


def queued_ids(plan):
    return {q.task_id for q in plan.queued}


def preempted_ids(plan):
    return {p.task_id for p in plan.preemptions}


def released_ids(plan):
    return {r.task_id for r in plan.releases}


class SharedPoolExhaustedTest(unittest.TestCase):
    """共享池耗尽时，各租户按公平份额授权，最低优先级任务排队。"""

    def setUp(self):
        self.cluster = Cluster(res(10, 10))
        self.tenants = [
            Tenant("A", res(2, 2), res(6, 6)),
            Tenant("B", res(2, 2), res(6, 6)),
        ]
        self.tasks = [
            Task("a1", "A", 5, res(2, 2)),
            Task("a2", "A", 4, res(2, 2)),
            Task("a3", "A", 3, res(2, 2)),
            Task("b1", "B", 5, res(2, 2)),
            Task("b2", "B", 4, res(2, 2)),
            Task("b3", "B", 3, res(2, 2)),
        ]

    def test_lowest_priority_queued_when_pool_exhausted(self):
        plan = arbitrate(self.cluster, self.tenants, self.tasks)
        self.assertEqual(grant_ids(plan), {"a1", "a2", "b1", "b2"})
        self.assertEqual(queued_ids(plan), {"a3", "b3"})
        self.assertEqual(preempted_ids(plan), set())

    def test_total_granted_within_capacity(self):
        plan = arbitrate(self.cluster, self.tenants, self.tasks)
        total = Resource()
        for g in plan.grants:
            total = total + g.resource
        self.assertTrue(total.fits(self.cluster.capacity))

    def test_every_decision_has_reason(self):
        plan = arbitrate(self.cluster, self.tenants, self.tasks)
        self.assertTrue(plan.explanations)
        for decision in plan.grants + plan.queued + plan.preemptions:
            self.assertTrue(decision.reason.strip())


class HighPriorityArrivalTest(unittest.TestCase):
    """高优任务到达且共享池不足时，整体抢占低优任务，但不击穿最小保障。"""

    def setUp(self):
        self.cluster = Cluster(res(9, 9))
        self.tenants = [
            Tenant("A", res(2, 2), res(8, 8)),
            Tenant("B", res(2, 2), res(8, 8)),
        ]
        self.tasks = [
            Task("a1", "A", 1, res(2, 2), running=True),
            Task("a2", "A", 1, res(2, 2), running=True),
            Task("a3", "A", 1, res(1, 1)),
            Task("b_high", "B", 9, res(5, 5)),
        ]
        self.plan = arbitrate(self.cluster, self.tenants, self.tasks)

    def test_high_priority_task_granted_via_preemption(self):
        self.assertIn("b_high", grant_ids(self.plan))

    def test_preemption_is_atomic(self):
        # 被抢占任务整体释放：释放量等于其完整需求，不存在部分释放。
        pre = {p.task_id: p for p in self.plan.preemptions}
        self.assertEqual(set(pre), {"a1"})
        self.assertEqual(pre["a1"].freed, res(2, 2))
        self.assertNotIn("a1", grant_ids(self.plan))

    def test_min_guarantee_not_breached(self):
        # 租户 A 被抢占后仍保有最小保障 2cpu/2GB（a2 继续运行）。
        usage = self.plan.tenant_usage["A"]
        self.assertTrue(res(2, 2).fits(usage))
        self.assertIn("a2", grant_ids(self.plan))

    def test_lower_priority_pending_task_stays_queued(self):
        self.assertIn("a3", queued_ids(self.plan))

    def test_non_preemptible_task_never_preempted(self):
        tasks = [
            Task("a1", "A", 1, res(2, 2), running=True, preemptible=False),
            Task("a2", "A", 1, res(2, 2), running=True),
            Task("b_high", "B", 9, res(5, 5)),
        ]
        plan = arbitrate(self.cluster, self.tenants, tasks)
        self.assertNotIn("a1", preempted_ids(plan))
        self.assertIn("a1", grant_ids(plan))

    def test_deterministic_output(self):
        again = arbitrate(self.cluster, self.tenants, self.tasks)
        self.assertEqual(self.plan, again)


class TenantOverQuotaTest(unittest.TestCase):
    """租户需求超过最大配额时，授权被限制在上限内。"""

    def test_grant_capped_at_max_quota(self):
        cluster = Cluster(res(20, 20))
        tenants = [Tenant("A", res(2, 2), res(5, 5))]
        tasks = [Task(f"a{i}", "A", 5, res(1, 1)) for i in range(8)]
        plan = arbitrate(cluster, tenants, tasks)
        self.assertEqual(len(plan.grants), 5)
        self.assertEqual(len(plan.queued), 3)
        self.assertTrue(plan.tenant_usage["A"].fits(res(5, 5)))

    def test_preemption_never_exceeds_max_quota(self):
        cluster = Cluster(res(10, 10))
        tenants = [
            Tenant("A", res(0, 0), res(4, 4)),
            Tenant("B", res(2, 2), res(8, 8)),
        ]
        tasks = [
            Task("b1", "B", 1, res(6, 6), running=True),
            Task("a_hi", "A", 9, res(6, 6)),
        ]
        plan = arbitrate(cluster, tenants, tasks)
        # a_hi 优先级虽高，但授权会突破租户 A 的最大配额，只能排队。
        self.assertIn("a_hi", queued_ids(plan))
        self.assertEqual(preempted_ids(plan), set())
        self.assertTrue(plan.tenant_usage["A"].fits(res(4, 4)))


class CapacityChangeTest(unittest.TestCase):
    """节点容量变化：缩容时按优先级从低到高释放，且不破最小保障。"""

    def setUp(self):
        self.tenants = [
            Tenant("A", res(3, 3), res(8, 8)),
            Tenant("B", res(3, 3), res(8, 8)),
        ]
        self.tasks = [
            Task("a_hi", "A", 9, res(3, 3), running=True),
            Task("a_lo", "A", 1, res(3, 3), running=True),
            Task("b_hi", "B", 9, res(3, 3), running=True),
            Task("b_lo", "B", 1, res(3, 3), running=True),
        ]

    def test_shrink_releases_lowest_priority_first(self):
        plan = arbitrate(Cluster(res(8, 8)), self.tenants, self.tasks)
        self.assertEqual(released_ids(plan), {"a_lo", "b_lo"})
        self.assertEqual(grant_ids(plan), {"a_hi", "b_hi"})

    def test_shrink_never_below_min_guarantee(self):
        plan = arbitrate(Cluster(res(8, 8)), self.tenants, self.tasks)
        for tenant in self.tenants:
            usage = plan.tenant_usage[tenant.name]
            self.assertTrue(tenant.min_guarantee.fits(usage))

    def test_shrink_below_total_min_guarantee_is_infeasible(self):
        with self.assertRaises(InfeasibleError):
            arbitrate(Cluster(res(5, 5)), self.tenants, self.tasks)


class ZeroQuotaTest(unittest.TestCase):
    """零配额租户得不到任何资源，即使任务优先级最高。"""

    def test_zero_quota_tenant_gets_nothing(self):
        cluster = Cluster(res(10, 10))
        tenants = [
            Tenant("A", res(0, 0), res(0, 0)),
            Tenant("B", res(2, 2), res(6, 6)),
        ]
        tasks = [
            Task("a1", "A", 9, res(1, 1)),
            Task("b1", "B", 1, res(2, 2)),
        ]
        plan = arbitrate(cluster, tenants, tasks)
        self.assertEqual(grant_ids(plan), {"b1"})
        self.assertEqual(queued_ids(plan), {"a1"})
        self.assertEqual(plan.tenant_usage["A"], res(0, 0))
        self.assertIn("配额", plan.queued[0].reason)


if __name__ == "__main__":
    unittest.main()
