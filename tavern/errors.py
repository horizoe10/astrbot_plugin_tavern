"""统一错误分类与失败上报助手（v0.12.0）。

背景（对应规划 F2）：历史上「空 message_id 误判发送成功」「定时器轮询异常被静默
吞掉」等问题，根源在于关键路径缺少统一的失败语义：哪些是预期内可恢复的瞬时错误、
哪些是应上抛的数据一致性错误、哪些是预期内的策略拒绝。

本模块提供：
- ``TavernError``：插件错误基类，派生语义分类。
- ``TransientError``：瞬时错误（网络抖动、模型超时、平台限流）——按 warning 记录，
  允许重试与降级。
- ``DataIntegrityError``：数据一致性错误（并发冲突、脏状态）——应上抛并由调用方
  决定回滚，日志按 exception 记录。
- ``PolicyRejection``：预期内的策略拒绝（权限、白名单、配额、状态机非法跳转）——
  按 info 记录，不视为故障。
- ``report_failure()``：统一失败上报入口：按语义分类记录日志、返回归一化消息，
  便于富消息发送 / 模型调用 / DB 写入三类关键路径保持一致的可观测性。

用法示例：:

    from .errors import report_failure, TransientError

    try:
        await fn(...)
    except Exception as exc:
        report_failure(
            stage="rich_send", operation="ark",
            exc=exc, transient=True, context={"origin": origin},
        )
        # 继续降级逻辑……
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from astrbot.api import logger

__all__ = [
    "TavernError",
    "TransientError",
    "DataIntegrityError",
    "PolicyRejection",
    "EconomyDisabledError",
    "InsufficientFundsError",
    "EconomyConflictError",
    "report_failure",
]


class TavernError(Exception):
    """AI 酒馆插件错误基类。"""


class TransientError(TavernError):
    """瞬时、可恢复错误（网络抖动、模型超时、平台限流）。

    调用方应重试或降级，而不是把错误扩散给用户。
    """


class DataIntegrityError(TavernError):
    """数据一致性错误（并发冲突、脏状态、半提交）。

    调用方应中止当前操作并回滚，必要时交由上层恢复流程处理。
    """


class PolicyRejection(TavernError):
    """预期内的策略拒绝（权限、白名单、配额、非法状态跳转）。

    这是正常业务分支，不属于故障，无需告警。
    """


class EconomyDisabledError(PolicyRejection):
    """经济系统未启用或世界包未接入。"""


class InsufficientFundsError(PolicyRejection):
    """余额不足（货币不允许负数时）。"""


class EconomyConflictError(DataIntegrityError):
    """经济操作冲突（重复 operation_id 与既有结果不一致等）。"""


def _classify(exc: BaseException) -> str:
    """把异常映射到瞬时错误、数据一致性错误、策略拒绝或未知错误。"""
    if isinstance(exc, TransientError):
        return "transient"
    if isinstance(exc, DataIntegrityError):
        return "integrity"
    if isinstance(exc, PolicyRejection):
        return "policy"
    return "unknown"


def report_failure(
    *,
    stage: str,
    operation: str,
    exc: BaseException,
    context: Optional[Mapping[str, Any]] = None,
    transient: Optional[bool] = None,
) -> str:
    """统一失败上报：按语义分类记录日志并返回归一化消息。

    参数：
        stage: 失败发生的阶段，如 ``rich_send`` / ``model_call`` / ``db_write``。
        operation: 具体操作，如 ``ark`` / ``llm_generate`` / ``cast_vote``。
        exc: 捕获到的异常。
        context: 附加上下文键值（不含密钥；调用方自行保证脱敏）。
        transient: 显式覆盖瞬时性分类（如 ``True`` 表示可重试）。
    """
    category = _classify(exc)
    if transient is not None:
        category = "transient" if transient else "integrity"
    details = "".join(
        f" {key}={value}" for key, value in (context or {}).items()
    )
    message = f"AI 酒馆 {stage}.{operation} 失败（{category}）"
    if details:
        message += f" [{details.strip()}]"
    if category == "policy":
        logger.info("%s: %s", message, exc)
    elif category == "transient":
        logger.warning("%s: %s", message, exc)
    else:
        logger.exception("%s: %s", message, exc)
    return message
