"""引擎层单元测试。

覆盖：测试执行器（步骤/断言/超时/取消）、cron、环境依赖解析、
覆盖率、报告生成、缺陷、通知。
"""

from __future__ import annotations

import datetime
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import (CoverageAnalyzer, CronSchedule, DefectManager,
                    EnvironmentManager, NotificationManager, ReportGenerator,
                    TestExecutor, cron_matches, parse_cron)
from engine.executor import evaluate_assertion, resolve_expr, safe_eval
from storage import BuildStoreRegistry, StoreRegistry


class TestResolveExpr(unittest.TestCase):
    def test_pure_reference_returns_value(self):
        self.assertEqual(resolve_expr("${a.b}", {"a": {"b": 42}}), 42)

    def test_partial_substitution(self):
        self.assertEqual(resolve_expr("x=${a}", {"a": "hi"}), "x=hi")

    def test_missing_path_returns_none(self):
        self.assertIsNone(resolve_expr("${a.b.c}", {"a": {}}))

    def test_list_index(self):
        self.assertEqual(resolve_expr("${items.0}", {"items": ["x", "y"]}), "x")


class TestSafeEval(unittest.TestCase):
    def test_arithmetic(self):
        self.assertEqual(safe_eval("2 + 3 * 4", {}), 14)

    def test_forbidden_import(self):
        with self.assertRaises(Exception):
            safe_eval("__import__('os')", {})

    def test_forbidden_attribute(self):
        with self.assertRaises(Exception):
            safe_eval("().__class__", {})


class TestEvaluateAssertion(unittest.TestCase):
    def test_equals_with_string_number(self):
        ok, _ = evaluate_assertion("equals", 14, "14")
        self.assertTrue(ok)

    def test_between(self):
        ok, _ = evaluate_assertion("between", 14, [10, 20])
        self.assertTrue(ok)
        ok, _ = evaluate_assertion("between", 5, [10, 20])
        self.assertFalse(ok)

    def test_regex(self):
        ok, _ = evaluate_assertion("regex", "release-2.31.0", r"^\d+\.\d+")
        # 注意：regex 比较的是 str(actual)
        ok, _ = evaluate_assertion("regex", "2.31.0-x", r"^\d+\.\d+")
        self.assertTrue(ok)

    def test_contains(self):
        ok, _ = evaluate_assertion("contains", {"ok": True}, "ok")
        self.assertTrue(ok)


class TestExecutorRun(unittest.TestCase):
    def test_passing_case(self):
        case = {
            "id": "c1", "name": "健康检查",
            "steps": [
                {"action": "request", "method": "GET", "url": "/api/health"},
                {"action": "assert", "type": "status", "actual": "${resp.status}", "expected": 200},
                {"action": "assert", "type": "truthy", "actual": "${resp.body.ok}", "expected": True},
            ],
        }
        result = TestExecutor().execute_case(case, {"latency_ms": 0})
        self.assertEqual(result["status"], "passed")

    def test_failing_case(self):
        case = {
            "id": "c2", "name": "失败",
            "steps": [
                {"action": "request", "method": "GET", "url": "/api/error"},
                {"action": "assert", "type": "status", "actual": "${resp.status}", "expected": 200},
            ],
        }
        result = TestExecutor().execute_case(case, {"latency_ms": 0})
        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(result["assertions"]), 1)
        self.assertFalse(result["assertions"][0]["ok"])

    def test_script_and_between(self):
        case = {
            "id": "c3", "name": "脚本",
            "steps": [
                {"action": "script", "expr": "2 + 3 * 4", "save_as": "r"},
                {"action": "assert", "type": "equals", "actual": "${r}", "expected": 14},
                {"action": "assert", "type": "between", "actual": "${r}", "expected": [10, 20]},
            ],
        }
        result = TestExecutor().execute_case(case, {})
        self.assertEqual(result["status"], "passed")

    def test_timeout(self):
        case = {
            "id": "c4", "name": "超时", "timeout": 0.1,
            "steps": [
                {"action": "sleep", "seconds": 0.05},
                {"action": "sleep", "seconds": 0.05},
                {"action": "sleep", "seconds": 0.05},
            ],
        }
        result = TestExecutor().execute_case(case, {})
        self.assertEqual(result["status"], "timeout")

    def test_disabled_skipped(self):
        case = {"id": "c5", "name": "禁用", "enabled": False, "steps": []}
        result = TestExecutor().execute_case(case, {})
        self.assertEqual(result["status"], "skipped")

    def test_env_isolation_changes_result(self):
        """同一用例，高失败率环境与零失败率环境结果不同（环境隔离）。"""
        case = {
            "id": "c6", "name": "接口",
            "steps": [
                {"action": "request", "method": "GET", "url": "/api/health"},
                {"action": "assert", "type": "status", "actual": "${resp.status}", "expected": 200},
            ],
        }
        # 稳定环境：一定通过
        r1 = TestExecutor().execute_case(case, {"latency_ms": 0, "fail_rate": 0.0})
        self.assertEqual(r1["status"], "passed")
        # 失败率 1.0 的环境：一定失败
        r2 = TestExecutor().execute_case(case, {"latency_ms": 0, "fail_rate": 1.0})
        self.assertEqual(r2["status"], "failed")


class TestCron(unittest.TestCase):
    def test_parse_and_match(self):
        sched = parse_cron("*/10 * * * *")
        self.assertEqual(sched.minute, [0, 10, 20, 30, 40, 50])
        self.assertTrue(cron_matches("0 9 * * 1", datetime.datetime(2026, 10, 5, 9, 0)))
        self.assertFalse(cron_matches("0 9 * * 1", datetime.datetime(2026, 10, 6, 9, 0)))

    def test_invalid(self):
        with self.assertRaises(ValueError):
            parse_cron("* * *")


class TestEnvironments(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.registry = StoreRegistry(os.path.join(self.tmp.name, "store"))
        self.mgr = EnvironmentManager(self.registry, self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_resolve_dependencies(self):
        env = self.mgr.create("p1", {
            "name": "dev",
            "dependencies": [
                {"name": "requests", "constraint": ">=2.28"},
                {"name": "flask", "constraint": ">=3.0"},
                {"name": "numpy", "constraint": ">=99.0"},
            ],
        })
        resolved = self.mgr.resolve(env["id"])
        self.assertEqual(resolved["resolved_count"], 2)
        self.assertEqual(resolved["conflict_count"], 1)
        statuses = {d["name"]: d["status"] for d in resolved["dependencies"]}
        self.assertEqual(statuses["numpy"], "conflict")

    def test_workspace_isolation(self):
        e1 = self.mgr.create("p1", {"name": "a"})
        e2 = self.mgr.create("p1", {"name": "b"})
        self.assertNotEqual(self.mgr.workspace_dir(e1["id"]), self.mgr.workspace_dir(e2["id"]))
        self.assertTrue(os.path.isdir(self.mgr.workspace_dir(e1["id"])))

    def test_snapshot(self):
        env = self.mgr.create("p1", {"name": "dev", "variables": {"X": "1"}})
        snap = self.mgr.snapshot(env["id"])
        self.assertEqual(snap["variables"]["X"], "1")


class TestCoverage(unittest.TestCase):
    def test_stable_per_build(self):
        with tempfile.TemporaryDirectory() as d:
            reg = BuildStoreRegistry(os.path.join(d, "builds"))
            reg.for_project("p1").create("b1")
            cov = CoverageAnalyzer(reg)
            c1 = cov.generate("p1", "b1", 0.8)
            c2 = cov.generate("p1", "b1", 0.8)
            self.assertEqual(c1["percent"], c2["percent"])  # 确定性
            self.assertGreaterEqual(c1["percent"], 0)
            self.assertLessEqual(c1["percent"], 100)

    def test_trend(self):
        with tempfile.TemporaryDirectory() as d:
            reg = BuildStoreRegistry(os.path.join(d, "builds"))
            cov = CoverageAnalyzer(reg)
            for bid in ("b1", "b2"):
                reg.for_project("p1").create(bid)
                cov.generate("p1", bid, 0.5)
            self.assertEqual(len(cov.trend("p1")["points"]), 2)


class TestReport(unittest.TestCase):
    def test_report_metrics(self):
        with tempfile.TemporaryDirectory() as d:
            reg = BuildStoreRegistry(os.path.join(d, "builds"))
            store = reg.for_project("p1")
            store.create("b1")
            store.set_total("b1", 4)
            for i in range(4):
                store.record_result("b1", {"case_id": f"c{i}", "case_name": f"c{i}",
                                           "group": "g", "priority": "P1",
                                           "status": "passed" if i < 3 else "failed",
                                           "duration": 0.1 + i * 0.1, "logs": []})
            store.finish("b1", "failed")
            rep = ReportGenerator(reg).build_report("p1", "b1")
            self.assertEqual(rep["summary"]["passed"], 3)
            self.assertEqual(rep["summary"]["pass_rate"], 75.0)
            self.assertEqual(len(rep["failures"]), 1)
            self.assertAlmostEqual(rep["durations"]["max"], 0.4, places=3)


class TestDefects(unittest.TestCase):
    def test_create_from_case(self):
        with tempfile.TemporaryDirectory() as d:
            reg = StoreRegistry(os.path.join(d, "store"))
            mgr = DefectManager(reg)
            defect = mgr.create_from_case("p1", {
                "case_id": "c1", "case_name": "登录", "priority": "P0", "status": "failed",
                "assertions": [{"ok": False, "message": "期望 == 200"}], "steps": [],
            }, "b1")
            self.assertIsNotNone(defect)
            self.assertEqual(defect["source_case_id"], "c1")
            self.assertEqual(mgr.stats("p1")["total"], 1)


class _FakeBuildStore:
    """process_build 的最小构建存储替身：只提供 results()。"""

    def __init__(self, results):
        self._results = results

    def results(self, build_id):
        return self._results


def _build(bid, results, finished_at, status="passed"):
    return {"id": bid, "project_id": "p1", "status": status,
            "finished_at": finished_at}, _FakeBuildStore(results)


def _pass(case_id="c1"):
    return {"case_id": case_id, "case_name": case_id, "status": "passed"}


def _fail(case_id="c1", status="failed"):
    return {"case_id": case_id, "case_name": case_id, "status": status}


class TestDefectAutoClose(unittest.TestCase):
    """缺陷自动闭环：连续通过累计、抖动容错口径、自动重开与留痕。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.reg = StoreRegistry(os.path.join(self.tmp.name, "store"))
        self.mgr = DefectManager(self.reg)
        self.project = {
            "id": "p1", "auto_close_defects": True,
            "auto_close_required_passes": 3,
            "auto_close_target_status": "verified",
            "auto_close_flaky_tolerance": 1,
        }
        self.defect = self.mgr.create("p1", {
            "title": "登录偶发失败", "source_case_id": "c1", "source_build_id": "b0",
        })
        self.t0 = time.time()

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, bid, results, dt, status="passed"):
        build, store = _build(bid, results, self.t0 + dt, status=status)
        return self.mgr.process_build(self.project, build, store)

    def test_close_after_required_consecutive_passes(self):
        self.assertEqual(self._run("b1", [_pass()], 1), [])
        self.assertEqual(self._run("b2", [_pass()], 2), [])
        self.assertEqual(self.mgr.get(self.defect["id"])["auto_state"]["passes"], 2)
        transitions = self._run("b3", [_pass()], 3)
        self.assertEqual(len(transitions), 1)
        self.assertEqual(transitions[0]["to_status"], "verified")
        defect = self.mgr.get(self.defect["id"])
        self.assertEqual(defect["status"], "verified")
        self.assertEqual(defect["closed_by"], "auto")
        # 留痕：自动流转事件带原因与触发构建
        events = self.mgr.events(self.defect["id"])
        auto_events = [e for e in events if e["actor"] == "auto" and e["to_status"] == "verified"]
        self.assertEqual(len(auto_events), 1)
        self.assertIn("连续 3 场", auto_events[0]["reason"])
        self.assertEqual(auto_events[0]["build_id"], "b3")
        # 统计同步体现
        stats = self.mgr.stats("p1")
        self.assertEqual(stats["by_status"]["verified"], 1)
        self.assertEqual(stats["auto_closed"], 1)

    def test_flaky_tolerance_keeps_streak(self):
        """容错 1 场：P F P P 仍累计到 3 场并自动关闭。"""
        self._run("b1", [_pass()], 1)
        self._run("b2", [_fail()], 2, status="failed")   # 容错，不清零
        state = self.mgr.get(self.defect["id"])["auto_state"]
        self.assertEqual((state["passes"], state["tolerated"]), (1, 1))
        self._run("b3", [_pass()], 3)
        transitions = self._run("b4", [_pass()], 4)
        self.assertEqual(len(transitions), 1)
        self.assertEqual(self.mgr.get(self.defect["id"])["status"], "verified")

    def test_second_failure_exceeding_tolerance_resets(self):
        """同一窗口内第 2 场失败超出容错，计数清零重来。"""
        self._run("b1", [_pass()], 1)
        self._run("b2", [_fail()], 2, status="failed")   # 容错
        self._run("b3", [_fail()], 3, status="failed")   # 超出容错 → 清零
        state = self.mgr.get(self.defect["id"])["auto_state"]
        self.assertEqual((state["passes"], state["tolerated"]), (0, 0))
        self._run("b4", [_pass()], 4)
        self._run("b5", [_pass()], 5)
        self.assertEqual(self.mgr.get(self.defect["id"])["status"], "open")
        transitions = self._run("b6", [_pass()], 6)      # 重新累计满 3 场
        self.assertEqual(len(transitions), 1)
        self.assertEqual(self.mgr.get(self.defect["id"])["status"], "verified")

    def test_strict_mode_resets_immediately(self):
        """容错 0（严格模式）：任何一场失败立即清零。"""
        self.project["auto_close_flaky_tolerance"] = 0
        self._run("b1", [_pass()], 1)
        self._run("b2", [_pass()], 2)
        self._run("b3", [_fail()], 3, status="failed")
        self.assertEqual(self.mgr.get(self.defect["id"])["auto_state"]["passes"], 0)
        self._run("b4", [_pass()], 4)
        self._run("b5", [_pass()], 5)
        self.assertEqual(self.mgr.get(self.defect["id"])["status"], "open")

    def test_auto_reopen_when_case_fails_again(self):
        for i in range(1, 4):
            self._run(f"b{i}", [_pass()], i)
        self.assertEqual(self.mgr.get(self.defect["id"])["status"], "verified")
        transitions = self._run("b4", [_fail()], 4, status="failed")
        self.assertEqual(len(transitions), 1)
        self.assertEqual(transitions[0]["to_status"], "reopened")
        defect = self.mgr.get(self.defect["id"])
        self.assertEqual(defect["status"], "reopened")
        self.assertEqual(defect["reopened_by"], "auto")
        self.assertIsNone(defect["closed_by"])
        # 重开后计数清零，需重新累计
        self.assertEqual(defect["auto_state"]["passes"], 0)
        events = self.mgr.events(self.defect["id"])
        reopen = [e for e in events if e["to_status"] == "reopened"]
        self.assertEqual(reopen[0]["actor"], "auto")
        self.assertIn("再次失败", reopen[0]["reason"])

    def test_skipped_and_absent_case_not_counted(self):
        self._run("b1", [{"case_id": "c1", "status": "skipped"}], 1)
        self._run("b2", [_pass("other_case")], 2)  # 来源用例不在本场构建
        self.assertEqual(self.mgr.get(self.defect["id"])["auto_state"]["passes"], 0)

    def test_old_build_not_counted(self):
        """结束时间早于缺陷上次流转的构建不参与判定。"""
        build, store = _build("b_old", [_pass()], self.t0 - 100)
        self.assertEqual(self.mgr.process_build(self.project, build, store), [])
        self.assertEqual(self.mgr.get(self.defect["id"])["auto_state"]["passes"], 0)

    def test_cancelled_build_not_counted(self):
        build, store = _build("b_c", [_pass()], self.t0 + 1, status="cancelled")
        self.assertEqual(self.mgr.process_build(self.project, build, store), [])

    def test_same_build_not_counted_twice(self):
        self._run("b1", [_pass()], 1)
        self._run("b1", [_pass()], 1)  # 重复处理同一场构建
        self.assertEqual(self.mgr.get(self.defect["id"])["auto_state"]["passes"], 1)

    def test_disabled_project_noop(self):
        self.project["auto_close_defects"] = False
        for i in range(1, 5):
            self.assertEqual(self._run(f"b{i}", [_pass()], i), [])
        self.assertEqual(self.mgr.get(self.defect["id"])["status"], "open")

    def test_target_status_fixed(self):
        self.project["auto_close_target_status"] = "fixed"
        for i in range(1, 4):
            self._run(f"b{i}", [_pass()], i)
        self.assertEqual(self.mgr.get(self.defect["id"])["status"], "fixed")

    def test_manual_transition_distinguished(self):
        """人工流转与自动流转在留痕和统计中可区分。"""
        self.mgr.update(self.defect["id"], {"status": "closed"},
                        actor="manual", operator="qa-li", reason="确认已修复")
        defect = self.mgr.get(self.defect["id"])
        self.assertEqual(defect["closed_by"], "manual")
        events = self.mgr.events(self.defect["id"])
        manual = [e for e in events if e["to_status"] == "closed"]
        self.assertEqual(manual[0]["actor"], "manual")
        self.assertEqual(manual[0]["operator"], "qa-li")
        self.assertEqual(manual[0]["reason"], "确认已修复")
        stats = self.mgr.stats("p1")
        self.assertEqual(stats["resolved"], 1)
        self.assertEqual(stats["auto_closed"], 0)  # 人工关闭不计入自动关闭

    def test_manual_transition_resets_streak(self):
        """人工流转后连续通过重新累计（旧构建不再计入）。"""
        self._run("b1", [_pass()], 1)
        self._run("b2", [_pass()], 2)
        self.mgr.update(self.defect["id"], {"status": "in_progress"}, actor="manual")
        # 之前的 2 场通过已作废，需重新累计 3 场
        self._run("b3", [_pass()], 3)
        self._run("b4", [_pass()], 4)
        self.assertEqual(self.mgr.get(self.defect["id"])["status"], "in_progress")
        self._run("b5", [_pass()], 5)
        self.assertEqual(self.mgr.get(self.defect["id"])["status"], "verified")


class TestNotify(unittest.TestCase):
    def test_fire_and_events(self):
        with tempfile.TemporaryDirectory() as d:
            reg = StoreRegistry(os.path.join(d, "store"))
            mgr = NotificationManager(reg)
            integration = mgr.create("p1", {"type": "webhook", "config": {"url": "http://x"},
                                            "events": ["build.failed"]})
            fired = mgr.fire("p1", "build.passed", {"build_id": "b1"})
            self.assertEqual(fired, [])  # 未订阅 build.passed
            fired = mgr.fire("p1", "build.failed", {"build_id": "b1"})
            self.assertEqual(len(fired), 1)
            self.assertEqual(fired[0]["status"], "delivered")
            self.assertEqual(len(mgr.events("p1")), 1)

    def test_test_delivery_without_target(self):
        with tempfile.TemporaryDirectory() as d:
            reg = StoreRegistry(os.path.join(d, "store"))
            mgr = NotificationManager(reg)
            integration = mgr.create("p1", {"type": "webhook", "config": {}})
            result = mgr.send_test(integration["id"])
            self.assertEqual(result["status"], "failed")


if __name__ == "__main__":
    unittest.main()
