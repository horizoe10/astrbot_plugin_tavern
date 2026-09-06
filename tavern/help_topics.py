from __future__ import annotations

from collections.abc import Mapping
from typing import Any


TOPICS = {
    "建卡": (
        "【建卡帮助】\n\n"
        "群内发送：\n/团 加入\n\n"
        "取得席位与建卡码后，先主动私聊机器人一次，再按提示绑定。"
        "预设选项使用跨页不变的全局序号；多选请用逗号、顿号或空格分隔，"
        "例如：1，3，5，8。\n\n"
        "查看与修改：\n"
        "/团 当前步骤\n\n"
        "/团 下一批\n\n"
        "/团 查看选项 <全局序号>\n\n"
        "/团 上一步\n\n"
        "/团 修改 <字段名称>\n\n"
        "/团 修改角色名 <新名称>\n\n"
        "/团 修改昵称 <新昵称>\n\n"
        "/团 预览\n\n"
        "/团 重填数值\n\n"
        "AI 设定助手（由模型代写或扩写当前字段）：\n"
        "/团 随机\n\n"
        "/团 补全 <初始设定>\n\n"
        "网页建卡（浏览器逐项填写，含 AI 按钮；需开启独立面板）：\n"
        "/团 网页建卡\n\n"
        "草稿操作：\n"
        "/团 重新建卡\n\n"
        "/团 取消建卡\n\n"
        "/团 放弃席位 确认\n\n"
        "确认提交：\n"
        "/团 确认建卡"
    ),
    "回合": "【回合帮助】\n轮到你时发送 /团 选择 A，也可在字母后补充简短演绎。普通行动只代表尝试，结果由系统与主持人裁定。若选项异常，可用 /团 重整选项。",
    "投票": "【投票帮助】\n集体决策期间发送 /团 投票 A。投票不消耗个人行动回合；票数相同时按副本规则处理。",
    "管理": "【管理帮助】\n常用：开启、开始故事、暂停、恢复、存档、读档、回滚、强制下一位、顺序、审核。精确修复与诊断请在后台副本详情的“急救与诊断”中操作。",
    "回顾": "【回顾帮助】\n/团 回顾 最近一轮、最近一章、我的经历、任务线索、角色关系、请假摘要。未填写范围时会根据当前阶段给出简要回顾。",
    "战术": "【战术帮助】\n/团 战况 查看目标、区域、风险、预兆和行动额度。\n/团 行动 <说明> 建立草稿；也可使用防守、援助、撤退或谈判。\n活动冲突的声明阶段可直接回复自然文本修改草稿；只有 /团 确认行动 才会原子提交。",
}


def contextual_help(
    topic: str = "",
    *,
    session: Mapping[str, Any] | None = None,
    turn: Mapping[str, Any] | None = None,
    user_id: str = "",
    is_admin: bool = False,
) -> str:
    key = str(topic or "").strip()
    if key in TOPICS:
        return TOPICS[key]
    state = str((session or {}).get("state") or "closed")
    current_user = str((turn or {}).get("current_user_id") or "")
    if state == "preparing":
        lead = "当前处于准备阶段。\n" + TOPICS["建卡"]
    elif state == "running" and current_user == str(user_id):
        lead = "现在轮到你行动。\n" + TOPICS["回合"]
    elif state == "running":
        lead = "故事正在运行，请等待自己的行动回合；场外发言不会推进世界状态。"
    elif state == "paused":
        lead = "当前副本已暂停。管理员可恢复现场，玩家可以先查看回顾与角色状态。"
    else:
        lead = "当前没有正在推进的故事。可先查看世界列表并由管理员开启副本。"
    topics = "可继续查询：/团 帮助 建卡｜回合｜投票｜战术｜回顾"
    if is_admin:
        topics += "｜管理"
    return f"{lead}\n\n{topics}"
