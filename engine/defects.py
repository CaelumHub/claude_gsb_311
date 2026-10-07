"""缺陷跟踪与自动闭环。

缺陷与用例 / 构建关联：一次构建里失败的用例，可以一键（或自动）转成缺陷，
缺陷保留来源（``source_case_id`` / ``source_build_id``），方便从报告页跳回
缺陷页闭环处理。

状态流：``open -> in_progress -> fixed -> verified -> closed``，
以及 ``reopened`` 用于重新打开。

自动闭环（项目开关，见项目配置 ``auto_close_*`` 字段）
------------------------------------------------------
当缺陷关联了来源用例，每场构建收尾时，调度器会调用
:meth:`DefectManager.evaluate_build`，按该用例在**后续跨构建**的表现增量
判定（每场构建只评估一次，用 ``evaluated_builds`` 去重，重复触发幂等）：

- 用例结果 ``passed``：连续通过计数 ``auto_streak`` +1；达到项目配置的
  阈值（默认连续 3 场）后，缺陷自动流转到配置的目标状态
  （``verified`` / ``fixed`` / ``closed``，默认 ``verified``），并置
  ``auto_resolved=True`` 标记「本次闭环由系统完成」；
- 用例结果 ``failed``（断言失败，真实回归）：计数清零；若缺陷当前是
  **系统自动闭环**状态（``auto_resolved=True``），自动重开为
  ``reopened``，之后连续通过可再次自动闭环；
- 用例结果 ``error`` / ``timeout``：视为**环境抖动**（环境、网络、超时，
  非产品缺陷），**永不触发自动重开**。是否清零由项目口径决定：
  * ``strict``（默认，严格清零）：抖动一场，连续通过计数立即清零重来；
  * ``tolerate_once``（容错一次）：每个连续通过窗口内容错一次，计数保留，
    但窗口内再抖一次即清零；计数一旦清零（失败 / 二次抖动 / 重开），
    容错额度恢复。
- 用例在该构建未执行 / 被跳过，或构建被 ``cancelled``：**不参与判定**，
  既不算通过也不清零，避免取消构建和环境抖动干扰闭环口径。

所有权与防横跳
~~~~~~~~~~~~~~
- 系统自动闭环的缺陷（``auto_resolved=True``）才允许被系统自动重开；
  任何人工状态流转都会清除该标记 —— 人工关闭 / 验证的缺陷人类保有
  所有权，系统不会再自动改动，避免「已修复 ↔ 重新打开」反复横跳；
- 每次状态流转（自动或人工）都追加一条 ``history``：操作人类型
  （``system`` / ``manual``）、操作员、原因、关联构建与时间，追责可查；
- 缺陷 ``status`` 是唯一事实源，缺陷列表 / 统计 / 报告页全部同源读取，
  不会出现「页面已修复、统计还挂待处理」的分叉。
"""

from __future__ import annotations

from typing import Optional

from .models import (
    AUTO_CLOSE_TARGETS,
    COUNTED_CASE_STATUSES,
    DEFAULT_AUTO_CLOSE,
    DEFECT_STATUSES,
    FAIL_CASE_STATUSES,
    JITTER_CASE_STATUSES,
    JITTER_POLICIES,
    SEVERITIES,
    new_id,
    now,
)

# evaluated_builds 只保留最近若干场，用于幂等去重，避免列表无限增长
_EVALUATED_KEEP = 200

# 目标状态的中文说明（写进自动流转原因）
_TARGET_LABEL = {"verified": "已验证", "fixed": "已修复", "closed": "已关闭"}

SYSTEM_OPERATOR = "系统自动闭环"


class DefectManager:
    """缺陷管理。"""

    def __init__(self, registry):
        self._store = registry.store("defects")
        self._projects = registry.store("projects")

    # ------------------------------------------------------------------ 基本 CRUD
    def create(self, project_id: str, payload: dict) -> dict:
        severity = payload.get("severity", "major")
        if severity not in SEVERITIES:
            severity = "major"
        status = payload.get("status", "open")
        if status not in DEFECT_STATUSES:
            status = "open"
        defect = {
            "id": new_id("def"),
            "project_id": project_id,
            "title": payload.get("title", "未命名缺陷"),
            "description": payload.get("description", ""),
            "severity": severity,
            "status": status,
            "source_case_id": payload.get("source_case_id"),
            "source_build_id": payload.get("source_build_id"),
            "assignee": payload.get("assignee", ""),
            "tags": payload.get("tags") or [],
            # 自动闭环判定状态
            "auto_streak": 0,              # 来源用例当前连续通过场次数
            "auto_jitter_used": False,     # 当前连续窗口是否已用过容错额度
            "auto_resolved": False,        # 当前闭环状态是否由系统自动完成
            "resolved_by": None,           # 最近一次进入闭环状态的操作人类型
            "evaluated_builds": [],        # 已评估过的构建（幂等去重）
            "history": [],                 # 状态流转留痕
        }
        self._store.insert(defect)
        return defect

    def create_from_case(self, project_id: str, case_result: dict,
                         build_id: str) -> Optional[dict]:
        """从失败的用例结果自动生成缺陷。"""
        if case_result.get("status") not in ("failed", "error", "timeout"):
            return None
        reason = ""
        for a in case_result.get("assertions", []):
            if not a.get("ok"):
                reason = a.get("message", "")
                break
        if not reason:
            for s in case_result.get("steps", []):
                if s.get("status") in ("failed", "error"):
                    reason = s.get("message", "")
                    break
        return self.create(project_id, {
            "title": f"[自动] 用例失败: {case_result.get('case_name')}",
            "description": reason or "用例执行失败，请查看构建日志。",
            "severity": "major" if case_result.get("priority") in ("P0", "P1") else "minor",
            "source_case_id": case_result.get("case_id"),
            "source_build_id": build_id,
        })

    def list(self, project_id: str, status: str = None,
             severity: str = None) -> list[dict]:
        where = [("project_id", "eq", project_id)]
        if status:
            where.append(("status", "eq", status))
        if severity:
            where.append(("severity", "eq", severity))
        return self._store.query(where=where, order_by="created_at", order="desc")

    def get(self, defect_id: str) -> Optional[dict]:
        return self._store.get(defect_id)

    def update(self, defect_id: str, patch: dict,
               operator: str = "人工") -> Optional[dict]:
        """人工更新缺陷。

        仅当 ``status`` 真实变化时追加一条人工流转记录，并清除系统自动闭环
        标记、重置连续计数 —— 人工介入后缺陷由人类保有所有权。
        """
        defect = self._store.get(defect_id)
        if defect is None:
            return None
        # reason 只进流转留痕，不作为缺陷顶层字段持久化
        reason_text = patch.pop("reason", None)
        status = patch.get("status")
        if status is not None:
            if status not in DEFECT_STATUSES:
                patch["status"] = "open"
                status = "open"
            if status != defect.get("status"):
                history = list(defect.get("history") or [])
                history.append(self._history_entry(
                    "manual", operator, defect.get("status"), status,
                    reason_text or "人工流转状态",
                    build_id=None))
                patch["history"] = history
                # 人工接管：系统不再拥有该缺陷的自动闭环所有权
                patch["auto_resolved"] = False
                patch["resolved_by"] = "manual"
                patch["auto_streak"] = 0
                patch["auto_jitter_used"] = False
        return self._store.update(defect_id, patch)

    def delete(self, defect_id: str) -> bool:
        return self._store.delete(defect_id)

    def stats(self, project_id: str) -> dict:
        """缺陷统计。与缺陷列表 / 报告页同源（直接按缺陷 status 聚合）。"""
        defects = self.list(project_id)
        by_status: dict[str, int] = {}
        by_severity: dict[str, int] = {}
        auto_resolved = pending = 0
        for d in defects:
            st = d.get("status", "open")
            by_status[st] = by_status.get(st, 0) + 1
            by_severity[d.get("severity", "major")] = by_severity.get(d.get("severity", "major"), 0) + 1
            if d.get("auto_resolved"):
                auto_resolved += 1  # 当前处于系统自动闭环状态（verified/fixed/closed 均可）
            if st in ("open", "reopened", "in_progress"):
                pending += 1
        return {
            "total": len(defects),
            "by_status": by_status,
            "by_severity": by_severity,
            "auto_resolved": auto_resolved,  # 系统自动闭环数
            "pending": pending,              # 待人工处理数
        }

    # ------------------------------------------------------------------ 自动闭环配置
    def auto_close_config(self, project_id: str) -> Optional[dict]:
        """读取并归一化项目的自动闭环配置；项目不存在返回 None。"""
        project = self._projects.get(project_id)
        if project is None:
            return None
        cfg = dict(DEFAULT_AUTO_CLOSE)
        if project.get("auto_close_enabled") is not None:
            cfg["enabled"] = bool(project.get("auto_close_enabled"))
        threshold = project.get("auto_close_pass_threshold")
        if isinstance(threshold, int) and threshold >= 1:
            cfg["pass_threshold"] = min(threshold, 20)
        target = project.get("auto_close_target_status")
        if target in AUTO_CLOSE_TARGETS:
            cfg["target_status"] = target
        policy = project.get("auto_close_jitter_policy")
        if policy in JITTER_POLICIES:
            cfg["jitter_policy"] = policy
        return cfg

    # ------------------------------------------------------------------ 自动闭环判定
    def evaluate_build(self, project_id: str, build_id: str,
                       build_registry) -> list[dict]:
        """一场构建收尾后，评估该项目所有关联来源用例的缺陷。

        返回本场构建实际触发的自动流转列表（每项含 defect_id / from / to /
        reason / build_id），供调度器写构建日志与发通知。开关关闭时直接
        返回空（不标记已评估，便于以后开启时回溯历史构建累计判定）。
        """
        cfg = self.auto_close_config(project_id)
        if cfg is None or not cfg["enabled"]:
            return []
        store = build_registry.for_project(project_id)
        transitions: list[dict] = []
        for defect in self._store.query(where=[("project_id", "eq", project_id)]):
            if not defect.get("source_case_id"):
                continue
            try:
                t = self._evaluate_one(defect, build_id, store, cfg)
            except Exception:  # noqa: BLE001 — 判定异常不影响构建收尾主流程
                continue
            if t:
                transitions.append(t)
        return transitions

    def _evaluate_one(self, defect: dict, build_id: str, store, cfg: dict) -> Optional[dict]:
        defect_id = defect["id"]
        evaluated = list(defect.get("evaluated_builds") or [])
        if build_id in evaluated:
            return None  # 幂等：这场构建已评估过

        # 先记账，保证同一场构建无论是否参与判定都不会被重复评估
        mark = {"evaluated_builds": (evaluated + [build_id])[-_EVALUATED_KEEP:]}

        build = store.get(build_id)
        if build is None or build.get("status") in ("pending", "running", "cancelled"):
            self._store.update(defect_id, mark)
            return None  # 构建不存在 / 未结束 / 被取消：不参与判定

        # 来源构建本身（产生缺陷的失败）不参与「后续构建连续通过」判定
        if build_id == defect.get("source_build_id"):
            self._store.update(defect_id, mark)
            return None

        records = store.results(
            build_id, where=[("case_id", "eq", defect.get("source_case_id"))])
        if not records:
            self._store.update(defect_id, mark)  # 用例本场未执行：不参与
            return None
        case_status = records[0].get("status")
        if case_status not in COUNTED_CASE_STATUSES:
            self._store.update(defect_id, mark)  # skipped 等：不参与
            return None

        status_now = defect.get("status", "open")
        patch = dict(mark)
        history = list(defect.get("history") or [])
        transition = None

        if case_status == "passed":
            # 已由系统自动闭环：冻结计数，重复通过不再重复流转
            if defect.get("auto_resolved") and status_now in AUTO_CLOSE_TARGETS:
                self._store.update(defect_id, patch)
                return None
            streak = int(defect.get("auto_streak") or 0) + 1
            patch["auto_streak"] = streak
            target = cfg["target_status"]
            # 人工已置 verified/closed（auto_resolved=False）的缺陷归人工所有，
            # 系统不得再动；fixed 视为「待验证」，连续通过可自动升级。
            human_locked = status_now in ("verified", "closed") and not defect.get("auto_resolved")
            if streak >= cfg["pass_threshold"] and target != status_now and not human_locked:
                patch.update({
                    "status": target,
                    "auto_resolved": True,
                    "resolved_by": "system",
                    "auto_jitter_used": False,
                })
                reason = (f"来源用例在后续构建中连续 {streak} 次通过"
                          f"（最近构建 {build_id}），系统自动置为「{_TARGET_LABEL[target]}」")
                history.append(self._history_entry(
                    "system", SYSTEM_OPERATOR, status_now, target, reason, build_id,
                    streak=streak, jitter_policy=cfg["jitter_policy"]))
                patch["history"] = history
                transition = self._transition(defect, status_now, target, reason, build_id)

        elif case_status in JITTER_CASE_STATUSES:
            # 环境抖动（error/timeout）：永不自动重开，只影响连续通过计数
            if defect.get("auto_resolved") and status_now in AUTO_CLOSE_TARGETS:
                pass  # 已闭环后的抖动不改动计数
            elif cfg["jitter_policy"] == "strict":
                patch.update(auto_streak=0, auto_jitter_used=False)
            elif defect.get("auto_jitter_used"):
                # 容错额度本窗口已用过：第二次抖动清零重来
                patch.update(auto_streak=0, auto_jitter_used=False)
            else:
                # 容错一次：计数保留，标记本窗口容错已用
                patch["auto_jitter_used"] = True

        elif case_status in FAIL_CASE_STATUSES:
            # 真实回归：计数清零；仅系统自动闭环的缺陷自动重开
            patch.update(auto_streak=0, auto_jitter_used=False)
            if defect.get("auto_resolved") and status_now in AUTO_CLOSE_TARGETS:
                patch.update({
                    "status": "reopened",
                    "auto_resolved": False,
                    "resolved_by": None,
                })
                reason = (f"来源用例在构建 {build_id} 中再次失败（真实回归），"
                          f"系统自动重新打开")
                history.append(self._history_entry(
                    "system", SYSTEM_OPERATOR, status_now, "reopened", reason, build_id))
                patch["history"] = history
                transition = self._transition(
                    defect, status_now, "reopened", reason, build_id)

        self._store.update(defect_id, patch)
        return transition

    # ------------------------------------------------------------------ 留痕工具
    @staticmethod
    def _history_entry(actor: str, operator: str, from_status: Optional[str],
                       to_status: str, reason: str, build_id: Optional[str],
                       **extra) -> dict:
        entry = {
            "id": new_id("dhis"),
            "actor": actor,           # system / manual
            "operator": operator,     # 可读的操作员（系统自动闭环 / 用户名）
            "from_status": from_status,
            "to_status": to_status,
            "reason": reason,
            "build_id": build_id,
            "at": now(),
        }
        entry.update(extra)
        return entry

    @staticmethod
    def _transition(defect: dict, from_status: str, to_status: str,
                    reason: str, build_id: str) -> dict:
        return {
            "defect_id": defect["id"],
            "project_id": defect.get("project_id"),
            "title": defect.get("title"),
            "from_status": from_status,
            "to_status": to_status,
            "reason": reason,
            "build_id": build_id,
        }
