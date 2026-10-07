"""领域模型：枚举常量、id 生成与通用工具。"""

from __future__ import annotations

import time
import uuid

# 用例优先级
PRIORITIES = ["P0", "P1", "P2", "P3"]

# 用例结果状态（单条）
CASE_STATUSES = ["passed", "failed", "error", "skipped", "timeout"]

# 构建状态（一次执行）
BUILD_STATUSES = ["pending", "running", "passed", "failed", "cancelled", "error"]

# 缺陷严重级别与状态流
SEVERITIES = ["blocker", "critical", "major", "minor", "trivial"]
DEFECT_STATUSES = ["open", "in_progress", "fixed", "verified", "closed", "reopened"]

# 缺陷操作人类型：system = 系统自动流转（可被自动重开），manual = 人工操作（人类保有所有权）
DEFECT_ACTORS = ["system", "manual"]

# 缺陷自动闭环目标状态：verified = 已验证（默认，保守口径），fixed = 已修复，closed = 已关闭
AUTO_CLOSE_TARGETS = ["verified", "fixed", "closed"]

# 环境抖动口径：strict = 抖动即清零；tolerate_once = 每个连续通过窗口容错一次
JITTER_POLICIES = ["strict", "tolerate_once"]

# 环境抖动对应的用例结果：error / timeout 多为环境、网络、超时问题，
# 与断言失败 failed（真实回归）区分开。
JITTER_CASE_STATUSES = ("error", "timeout")
FAIL_CASE_STATUSES = ("failed",)
# 参与连续判定的「有效」用例结果；skipped 不参与
COUNTED_CASE_STATUSES = ("passed", "failed", "error", "timeout")

# 自动闭环默认配置（项目级开关，可按项目覆盖）
DEFAULT_AUTO_CLOSE = {
    "enabled": False,
    "pass_threshold": 3,
    "jitter_policy": "strict",
    "target_status": "verified",
}

# 通知集成类型
INTEGRATION_TYPES = ["webhook", "slack", "email", "dingtalk"]

# 触发来源
TRIGGER_TYPES = ["manual", "schedule", "webhook", "ci"]


def new_id(prefix: str) -> str:
    """生成带前缀的唯一 id（时间戳 + 随机后缀，便于阅读与排查）。"""
    return f"{prefix}_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"


def now() -> float:
    return time.time()
