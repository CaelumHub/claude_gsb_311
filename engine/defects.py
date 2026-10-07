"""缺陷跟踪与自动闭环。

缺陷与用例 / 构建关联：一次构建里失败的用例，可以一键（或自动）转成缺陷，
缺陷保留来源（``source_case_id`` / ``source_build_id``），方便从报告页跳回
缺陷页闭环处理。

状态流：``open -> in_progress -> fixed -> verified -> closed``，
以及 ``reopened`` 用于重新打开。

缺陷自动闭环
------------
按项目开关（项目字段 ``auto_close_defects``）启用，只作用于关联了来源用例
（``source_case_id``）的缺陷，在每场构建收尾时评估一次：

- **自动关闭**：来源用例在后续构建中「连续通过 N 场」（跨构建累计，场数
  由 ``auto_close_required_passes`` 配置，默认 3）后，缺陷自动流转到目标
  状态（``auto_close_target_status``：``verified`` 或 ``fixed``）；
- **自动重开**：已解决（fixed / verified / closed）的缺陷，其来源用例在
  之后的构建中再次失败（failed / error / timeout）时，自动重新打开为
  ``reopened``，连续通过计数清零、重新累计；
- **抖动容错口径**：累计窗口内允许最多 ``auto_close_flaky_tolerance`` 场
  失败不清零（该场既不计入通过数、也不打断累计，默认容错 1 场）；超过
  容错场数则连续计数清零重来。容错设为 0 即严格模式：任何一场失败立即
  清零；
- **不计入的情形**：用例在该构建中未执行或 skipped、构建被取消、构建结束
  时间早于缺陷上次流转时间 —— 都不算通过也不算失败，计数原地不动；
- **留痕**：每次状态流转（自动或人工）都写入 ``defect_events``，记录操作
  者（``auto`` / ``manual``）、操作人、原因与触发构建；缺陷上的
  ``closed_by`` / ``reopened_by`` 标记最近一次关闭 / 重开的方式，统计与
  页面据此区分自动关闭与人工关闭。
"""

from __future__ import annotations

import threading
import time
from typing import Optional

from .models import (DEFECT_ACTORS, DEFECT_AUTO_CLOSE_TARGETS,
                     DEFECT_RESOLVED_STATUSES, DEFECT_STATUSES, SEVERITIES,
                     new_id)

# 未解决状态：自动关闭只对这些状态生效（target 为 verified 时另含 fixed）
_UNRESOLVED = ("open", "in_progress", "reopened")

# 用例失败类结果状态
_CASE_FAILED = ("failed", "error", "timeout")


def _int_or(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def auto_close_config(project: dict) -> dict:
    """从项目记录解析自动闭环配置（带默认值与合法值收敛）。"""
    project = project or {}
    target = project.get("auto_close_target_status")
    if target not in DEFECT_AUTO_CLOSE_TARGETS:
        target = "verified"
    return {
        "enabled": bool(project.get("auto_close_defects")),
        "required_passes": max(1, _int_or(project.get("auto_close_required_passes"), 3)),
        "target": target,
        "flaky_tolerance": max(0, _int_or(project.get("auto_close_flaky_tolerance"), 1)),
    }


def _fresh_auto_state() -> dict:
    """新一轮连续通过累计的起点状态。"""
    return {"passes": 0, "tolerated": 0, "since": time.time(), "last_build_id": None}


class DefectManager:
    """缺陷管理（含自动闭环与流转留痕）。"""

    def __init__(self, registry):
        self._store = registry.store("defects")
        self._events = registry.store("defect_events")
        # 串行化自动闭环评估，避免多场构建同时收尾时并发改同一缺陷的计数
        self._auto_lock = threading.Lock()

    # ------------------------------------------------------------------ 创建
    def create(self, project_id: str, payload: dict, actor: str = "manual") -> dict:
        severity = payload.get("severity", "major")
        if severity not in SEVERITIES:
            severity = "major"
        defect = {
            "id": new_id("def"),
            "project_id": project_id,
            "title": payload.get("title", "未命名缺陷"),
            "description": payload.get("description", ""),
            "severity": severity,
            "status": payload.get("status", "open"),
            "source_case_id": payload.get("source_case_id"),
            "source_build_id": payload.get("source_build_id"),
            "assignee": payload.get("assignee", ""),
            "tags": payload.get("tags") or [],
            # 最近一次关闭 / 重开的方式（auto / manual / None），追责用
            "closed_by": None,
            "reopened_by": None,
            # 自动闭环累计状态：连续通过场数 / 已容错失败场数 / 计数起点 / 已见构建
            "auto_state": _fresh_auto_state(),
        }
        self._store.insert(defect)
        reason = payload.get("create_reason") or (
            "构建中用例失败，自动创建缺陷" if actor == "auto" else "人工创建缺陷")
        self._record_event(defect, None, defect["status"], actor,
                           reason=reason, build_id=defect.get("source_build_id"))
        return defect

    def create_from_case(self, project_id: str, case_result: dict,
                         build_id: str) -> Optional[dict]:
        """从失败的用例结果自动生成缺陷。"""
        if case_result.get("status") not in _CASE_FAILED:
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
        }, actor="auto")

    # ------------------------------------------------------------------ 查询
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

    # ------------------------------------------------------------------ 更新
    def update(self, defect_id: str, patch: dict, actor: str = "manual",
               operator: str = "", reason: str = "",
               build_id: str = None) -> Optional[dict]:
        """合并更新；状态变化会记录流转事件并重置自动闭环计数。"""
        status = patch.get("status")
        if status is not None and status not in DEFECT_STATUSES:
            patch["status"] = "open"
        old = self.get(defect_id)
        if old is None:
            return None
        updated = self._store.update(defect_id, patch)
        if updated and status is not None and status != old.get("status"):
            updated = self._after_transition(
                updated, old.get("status"), actor,
                reason or "人工修改状态", build_id=build_id, operator=operator)
        return updated

    def delete(self, defect_id: str) -> bool:
        return self._store.delete(defect_id)

    # ------------------------------------------------------------------ 流转留痕
    def _record_event(self, defect: dict, from_status, to_status: str,
                      actor: str, reason: str = "", build_id: str = None,
                      operator: str = "") -> dict:
        if actor not in DEFECT_ACTORS:
            actor = "manual"
        event = {
            "id": new_id("devt"),
            "defect_id": defect["id"],
            "project_id": defect.get("project_id"),
            "from_status": from_status,
            "to_status": to_status,
            "actor": actor,
            "operator": operator or "",
            "reason": reason or "",
            "build_id": build_id,
            "created_at": time.time(),
        }
        self._events.insert(event)
        return event

    def _after_transition(self, defect: dict, from_status, actor: str,
                          reason: str, build_id: str = None,
                          operator: str = "") -> dict:
        """状态流转后的统一收尾：标记关闭方式、重置连续计数、写事件。"""
        to_status = defect.get("status")
        patch = {"auto_state": _fresh_auto_state()}
        if to_status in DEFECT_RESOLVED_STATUSES:
            patch["closed_by"] = actor
            patch["reopened_by"] = None
        elif from_status in DEFECT_RESOLVED_STATUSES:
            patch["reopened_by"] = actor
            patch["closed_by"] = None
        updated = self._store.update(defect["id"], patch) or defect
        self._record_event(updated, from_status, to_status, actor,
                           reason=reason, build_id=build_id, operator=operator)
        return updated

    def _transition(self, defect: dict, to_status: str, actor: str,
                    reason: str, build_id: str = None) -> dict:
        """执行一次自动流转，返回流转摘要。"""
        from_status = defect.get("status")
        updated = self._store.update(defect["id"], {"status": to_status})
        self._after_transition(updated or dict(defect, status=to_status),
                               from_status, actor, reason, build_id=build_id)
        return {"defect_id": defect["id"], "from_status": from_status,
                "to_status": to_status, "reason": reason, "build_id": build_id}

    def events(self, defect_id: str, limit: int = 50) -> list[dict]:
        """单个缺陷的流转记录（新的在前）。"""
        return self._events.query(where=[("defect_id", "eq", defect_id)],
                                  order_by="created_at", order="desc", limit=limit)

    def project_events(self, project_id: str, limit: int = 100) -> list[dict]:
        """项目级流转记录（审计用，新的在前）。"""
        return self._events.query(where=[("project_id", "eq", project_id)],
                                  order_by="created_at", order="desc", limit=limit)

    # ------------------------------------------------------------------ 自动闭环
    def process_build(self, project: dict, build: dict, build_store) -> list[dict]:
        """构建收尾时评估一次自动闭环，返回本次发生的流转列表。

        只会处理「结束时间晚于缺陷上次流转」的构建；同一场构建对同一缺陷
        只计数一次（``auto_state.last_build_id`` 幂等去重）。
        """
        config = auto_close_config(project)
        if not config["enabled"]:
            return []
        if build.get("status") not in ("passed", "failed"):
            return []  # 取消 / 异常的构建不参与判定
        finished_at = build.get("finished_at")
        if not finished_at:
            return []
        project_id = project.get("id") or build.get("project_id")
        build_id = build.get("id")

        # 本构建中各用例的最终结果（同一用例多条取最后一条）
        by_case: dict[str, dict] = {}
        for r in build_store.results(build_id):
            if r.get("case_id"):
                by_case[r["case_id"]] = r

        transitions: list[dict] = []
        with self._auto_lock:
            defects = self._store.query(where=[("project_id", "eq", project_id)])
            for defect in defects:
                case_id = defect.get("source_case_id")
                if not case_id:
                    continue
                state = defect.get("auto_state") or _fresh_auto_state()
                if finished_at < state.get("since", 0):
                    continue  # 该构建早于上次流转，不参与判定
                if state.get("last_build_id") == build_id:
                    continue  # 幂等：同一场构建不重复计数
                state["last_build_id"] = build_id

                result = by_case.get(case_id)
                if result is None or result.get("status") not in (
                        "passed",) + _CASE_FAILED:
                    # 用例未执行 / skipped：不算通过也不算失败，仅标记已见
                    self._store.update(defect["id"], {"auto_state": state})
                    continue

                status = defect.get("status", "open")
                if result.get("status") == "passed":
                    eligible = status in _UNRESOLVED or (
                        status == "fixed" and config["target"] == "verified")
                    if not eligible:
                        # 已解决状态下通过：仅推进已见构建
                        self._store.update(defect["id"], {"auto_state": state})
                        continue
                    state["passes"] = state.get("passes", 0) + 1
                    if state["passes"] >= config["required_passes"]:
                        reason = (f"来源用例连续 {state['passes']} 场构建通过"
                                  f"（阈值 {config['required_passes']} 场，"
                                  f"构建 {build_id}）")
                        transitions.append(self._transition(
                            defect, config["target"], "auto", reason,
                            build_id=build_id))
                    else:
                        self._store.update(defect["id"], {"auto_state": state})
                else:
                    # 失败类结果
                    if status in DEFECT_RESOLVED_STATUSES:
                        reason = (f"来源用例在构建 {build_id} 再次失败"
                                  f"（{result.get('status')}），自动重新打开")
                        transitions.append(self._transition(
                            defect, "reopened", "auto", reason,
                            build_id=build_id))
                        continue
                    # 未解决：按容错口径处理 —— 容错场数内不清零，超出则清零重来
                    if state.get("tolerated", 0) < config["flaky_tolerance"]:
                        state["tolerated"] = state.get("tolerated", 0) + 1
                    else:
                        state["passes"] = 0
                        state["tolerated"] = 0
                    self._store.update(defect["id"], {"auto_state": state})
        return transitions

    # ------------------------------------------------------------------ 统计
    def stats(self, project_id: str) -> dict:
        defects = self.list(project_id)
        by_status: dict[str, int] = {}
        by_severity: dict[str, int] = {}
        resolved = auto_closed = 0
        for d in defects:
            by_status[d.get("status", "open")] = by_status.get(d.get("status", "open"), 0) + 1
            by_severity[d.get("severity", "major")] = by_severity.get(d.get("severity", "major"), 0) + 1
            if d.get("status") in DEFECT_RESOLVED_STATUSES:
                resolved += 1
                if d.get("closed_by") == "auto":
                    auto_closed += 1
        return {"total": len(defects), "by_status": by_status,
                "by_severity": by_severity, "resolved": resolved,
                "auto_closed": auto_closed}
