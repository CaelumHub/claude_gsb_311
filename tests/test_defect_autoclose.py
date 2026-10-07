"""缺陷自动闭环判定测试。

覆盖口径：
- 连续 N 场通过（跨构建累计）后自动流转到项目配置的目标状态；
- 环境抖动（error/timeout）在 strict / tolerate_once 两种口径下的行为；
- 真实失败清零，并把系统自动闭环的缺陷自动重开（人工闭环的不动）；
- skipped / 未执行 / cancelled 构建不参与判定；
- 每场构建只评估一次（幂等）；
- 每次流转留痕（actor/operator/reason/build_id），自动与人工可区分；
- 统计与报告接口的缺陷状态同源同步。
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import DefectManager
from storage import BuildStoreRegistry, StoreRegistry


class AutoCloseTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.registry = StoreRegistry(os.path.join(self.tmp.name, "store"), shard_size=50)
        self.builds = BuildStoreRegistry(os.path.join(self.tmp.name, "builds"))
        self.projects = self.registry.store("projects")
        self.defects = DefectManager(self.registry)
        self.pid = self.projects.insert({"name": "P"})
        self.store = self.builds.for_project(self.pid)

    def tearDown(self):
        self.tmp.cleanup()

    def configure(self, **kw):
        patch = {
            "auto_close_enabled": True,
            "auto_close_pass_threshold": 3,
            "auto_close_target_status": "verified",
            "auto_close_jitter_policy": "strict",
        }
        patch.update(kw)
        self.projects.update(self.pid, patch)

    def new_build(self, case_status=None, build_status=None):
        """创建一场已结束构建并写入来源用例结果；case_status=None 表示用例未执行。"""
        bid = f"build_{time.time_ns()}"
        self.store.create(bid, suite_id="s1", env_id="e1", name="冒烟", trigger="manual")
        self.store.set_total(bid, 1 if case_status else 0)
        if case_status:
            self.store.record_result(bid, {
                "case_id": "case_x", "case_name": "来源用例", "group": "g",
                "priority": "P1", "status": case_status, "duration": 0.1,
                "steps": [], "assertions": [], "logs": [],
            })
        if build_status is None:
            build_status = "passed" if case_status == "passed" else "failed"
        self.store.finish(bid, build_status)
        return bid

    def evaluate(self, bid):
        return self.defects.evaluate_build(self.pid, bid, self.builds)

    def new_defect(self):
        return self.defects.create(self.pid, {
            "title": "用例失败", "source_case_id": "case_x",
            "source_build_id": "build_seed"})


class TestAutoResolve(AutoCloseTestBase):
    def test_consecutive_passes_across_builds_resolves(self):
        self.configure(auto_close_pass_threshold=3)
        d = self.new_defect()
        for i in range(2):
            self.assertEqual(self.evaluate(self.new_build("passed")), [])
            d = self.defects.get(d["id"])
            self.assertEqual(d["status"], "open")
            self.assertEqual(d["auto_streak"], i + 1)
        transitions = self.evaluate(self.new_build("passed"))
        d = self.defects.get(d["id"])
        self.assertEqual(d["status"], "verified")
        self.assertTrue(d["auto_resolved"])
        self.assertEqual(d["resolved_by"], "system")
        self.assertEqual(len(transitions), 1)
        self.assertEqual(transitions[0]["to_status"], "verified")

    def test_target_fixed_and_closed(self):
        for target in ("fixed", "closed"):
            self.configure(auto_close_pass_threshold=1, auto_close_target_status=target)
            d = self.new_defect()
            self.evaluate(self.new_build("passed"))
            self.assertEqual(self.defects.get(d["id"])["status"], target)

    def test_nonconsecutive_passes_do_not_resolve(self):
        self.configure(auto_close_pass_threshold=3)
        d = self.new_defect()
        self.evaluate(self.new_build("passed"))
        self.evaluate(self.new_build("passed"))
        self.evaluate(self.new_build("failed"))
        self.evaluate(self.new_build("passed"))
        d = self.defects.get(d["id"])
        self.assertEqual(d["status"], "open")
        self.assertEqual(d["auto_streak"], 1)

    def test_disabled_project_does_nothing(self):
        # 默认开关关闭：即使连续通过也不流转
        d = self.new_defect()
        for _ in range(5):
            self.evaluate(self.new_build("passed"))
        self.assertEqual(self.defects.get(d["id"])["status"], "open")
        # 开启后，只有开启之后结束的构建才参与连续判定
        self.configure(auto_close_pass_threshold=3)
        self.evaluate(self.new_build("passed"))
        self.evaluate(self.new_build("passed"))
        self.assertEqual(self.defects.get(d["id"])["status"], "open")
        self.evaluate(self.new_build("passed"))
        self.assertEqual(self.defects.get(d["id"])["status"], "verified")


class TestJitterPolicy(AutoCloseTestBase):
    def test_strict_jitter_resets_streak(self):
        self.configure(auto_close_pass_threshold=3, auto_close_jitter_policy="strict")
        d = self.new_defect()
        self.evaluate(self.new_build("passed"))
        self.evaluate(self.new_build("passed"))
        # timeout 视为环境抖动：严格口径清零
        self.evaluate(self.new_build("timeout"))
        d = self.defects.get(d["id"])
        self.assertEqual(d["auto_streak"], 0)
        self.assertEqual(d["status"], "open")  # 抖动不重开

    def test_tolerate_once_keeps_streak(self):
        self.configure(auto_close_pass_threshold=3, auto_close_jitter_policy="tolerate_once")
        d = self.new_defect()
        self.evaluate(self.new_build("passed"))
        self.evaluate(self.new_build("error"))
        d = self.defects.get(d["id"])
        self.assertEqual(d["auto_streak"], 1)
        self.assertTrue(d["auto_jitter_used"])
        self.evaluate(self.new_build("passed"))
        self.evaluate(self.new_build("passed"))
        d = self.defects.get(d["id"])
        self.assertEqual(d["status"], "verified")  # 容错一次后照常闭环

    def test_tolerate_once_second_jitter_resets(self):
        self.configure(auto_close_pass_threshold=3, auto_close_jitter_policy="tolerate_once")
        d = self.new_defect()
        self.evaluate(self.new_build("passed"))
        self.evaluate(self.new_build("error"))
        self.assertEqual(self.defects.get(d["id"])["auto_streak"], 1)
        self.evaluate(self.new_build("timeout"))
        d = self.defects.get(d["id"])
        self.assertEqual(d["auto_streak"], 0)        # 第二次抖动清零
        self.assertFalse(d["auto_jitter_used"])       # 额度恢复

    def test_jitter_does_not_reopen_resolved(self):
        self.configure(auto_close_pass_threshold=1, auto_close_jitter_policy="strict")
        d = self.new_defect()
        self.evaluate(self.new_build("passed"))
        self.assertEqual(self.defects.get(d["id"])["status"], "verified")
        # 闭环后抖动：不重开、不流转
        transitions = self.evaluate(self.new_build("timeout"))
        self.assertEqual(transitions, [])
        self.assertEqual(self.defects.get(d["id"])["status"], "verified")


class TestReopenAndOwnership(AutoCloseTestBase):
    def test_real_failure_reopens_auto_resolved(self):
        self.configure(auto_close_pass_threshold=1)
        d = self.new_defect()
        self.evaluate(self.new_build("passed"))
        self.assertEqual(self.defects.get(d["id"])["status"], "verified")
        transitions = self.evaluate(self.new_build("failed"))
        d = self.defects.get(d["id"])
        self.assertEqual(d["status"], "reopened")
        self.assertFalse(d["auto_resolved"])
        self.assertEqual(d["auto_streak"], 0)
        self.assertEqual(transitions[0]["to_status"], "reopened")
        # 再次连续通过可重新自动闭环（不横跳：每条状态都有明确触发）
        self.evaluate(self.new_build("passed"))
        self.assertEqual(self.defects.get(d["id"])["status"], "verified")

    def test_manual_closed_is_not_reopened(self):
        self.configure(auto_close_pass_threshold=1)
        d = self.new_defect()
        # 人工关闭：系统不拥有所有权
        self.defects.update(d["id"], {"status": "closed"}, operator="张三")
        d = self.defects.get(d["id"])
        self.assertFalse(d["auto_resolved"])
        self.evaluate(self.new_build("failed"))
        self.assertEqual(self.defects.get(d["id"])["status"], "closed")

    def test_manual_verified_not_touched_by_passes(self):
        self.configure(auto_close_pass_threshold=1, auto_close_target_status="verified")
        d = self.new_defect()
        self.defects.update(d["id"], {"status": "verified"}, operator="李四")
        for _ in range(3):
            self.assertEqual(self.evaluate(self.new_build("passed")), [])
        self.assertEqual(self.defects.get(d["id"])["status"], "verified")

    def test_manual_intervention_clears_ownership_and_resets_streak(self):
        self.configure(auto_close_pass_threshold=2)
        d = self.new_defect()
        self.evaluate(self.new_build("passed"))
        self.assertEqual(self.defects.get(d["id"])["auto_streak"], 1)
        self.defects.update(d["id"], {"status": "in_progress"}, operator="王五")
        d = self.defects.get(d["id"])
        self.assertEqual(d["auto_streak"], 0)
        self.assertFalse(d["auto_resolved"])


class TestIgnoredBuilds(AutoCloseTestBase):
    def test_skipped_result_not_counted(self):
        self.configure(auto_close_pass_threshold=3)
        d = self.new_defect()
        self.evaluate(self.new_build("passed"))
        self.evaluate(self.new_build("passed"))
        # skipped：既不算通过也不清零
        self.evaluate(self.new_build("skipped", build_status="passed"))
        self.evaluate(self.new_build("passed"))
        self.assertEqual(self.defects.get(d["id"])["status"], "verified")

    def test_case_not_run_not_counted(self):
        self.configure(auto_close_pass_threshold=2)
        d = self.new_defect()
        self.evaluate(self.new_build("passed"))
        self.evaluate(self.new_build(None, build_status="passed"))
        self.evaluate(self.new_build("passed"))
        self.assertEqual(self.defects.get(d["id"])["status"], "verified")

    def test_cancelled_build_not_counted(self):
        self.configure(auto_close_pass_threshold=2)
        d = self.new_defect()
        self.evaluate(self.new_build("passed"))
        # 取消的构建里即便有 error 结果也不参与
        self.evaluate(self.new_build("error", build_status="cancelled"))
        self.evaluate(self.new_build("passed"))
        self.assertEqual(self.defects.get(d["id"])["status"], "verified")

    def test_idempotent_same_build_evaluated_once(self):
        self.configure(auto_close_pass_threshold=3)
        d = self.new_defect()
        bid = self.new_build("passed")
        self.evaluate(bid)
        self.evaluate(bid)  # 重复触发
        self.assertEqual(self.defects.get(d["id"])["auto_streak"], 1)


class TestHistoryAndStats(AutoCloseTestBase):
    def test_history_distinguishes_system_and_manual(self):
        self.configure(auto_close_pass_threshold=1)
        d = self.new_defect()
        self.evaluate(self.new_build("passed"))
        self.evaluate(self.new_build("failed"))
        self.defects.update(d["id"], {"status": "closed",
                                      "reason": "确认不复现"}, operator="赵六")
        d = self.defects.get(d["id"])
        actors = [(h["actor"], h["to_status"]) for h in d["history"]]
        self.assertIn(("system", "verified"), actors)
        self.assertIn(("system", "reopened"), actors)
        self.assertIn(("manual", "closed"), actors)
        manual = [h for h in d["history"] if h["actor"] == "manual"][0]
        self.assertEqual(manual["operator"], "赵六")
        self.assertEqual(manual["reason"], "确认不复现")
        for h in d["history"]:
            self.assertIn("at", h)  # 每次流转都有时间戳

    def test_stats_count_auto_resolved_and_status(self):
        self.configure(auto_close_pass_threshold=1)
        self.new_defect()
        d2 = self.defects.create(self.pid, {"title": "人工发现，无来源用例"})
        self.evaluate(self.new_build("passed"))
        stats = self.defects.stats(self.pid)
        self.assertEqual(stats["by_status"]["verified"], 1)
        self.assertEqual(stats["auto_resolved"], 1)
        self.assertEqual(stats["total"], 2)
        # 人工关闭后统计同步变化
        self.defects.update(d2["id"], {"status": "closed"}, operator="钱七")
        stats = self.defects.stats(self.pid)
        self.assertEqual(stats["by_status"]["closed"], 1)
        self.assertEqual(stats["by_status"].get("open", 0), 0)
        self.assertEqual(stats["auto_resolved"], 1)  # 人工关闭不计入自动闭环数


if __name__ == "__main__":
    unittest.main()
