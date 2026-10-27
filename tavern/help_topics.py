from __future__ import annotations

from collections.abc import Mapping
from typing import Any


TOPICS = {
    "建卡": "【建卡帮助】\n群内发送 /酒馆 加入 获取席位与建卡码；先主动私聊机器人一次，再按当前步骤填写。预设只会在对应步骤分页显示，可回复本页序号、名称或稳定ID；发送“下一页/上一页”翻页。可用 /酒馆 上一步、/酒馆 修改 <字段>、/酒馆 当前步骤、/酒馆 预览、/酒馆 重填数值、/酒馆 确认建卡、/酒馆 取消建卡。",
    "回合": "【回合帮助】\n轮到你时发送 /酒馆 选择 A，也可在字母后补充简短演绎。普通行动只代表尝试，结果由酒馆裁定。若选项异常，可用 /酒馆 重整选项。",
    "投票": "【投票帮助】\n集体决策期间发送 /酒馆 投票 A。投票不消耗个人行动回合；票数相同时按副本规则处理。",
    "管理": "【管理帮助】\n常用：开启、开始故事、暂停、恢复、存档、读档、回滚、强制下一位、顺序、审核。精确修复与诊断请在后台副本详情的“急救与诊断”中操作。",
    "回顾": "【回顾帮助】\n/酒馆 回顾 最近一轮、最近一章、我的经历、任务线索、NPC关系、请假摘要。未填写范围时会根据当前阶段给出简要回顾。",
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
    topics = "可继续查询：/酒馆 帮助 建卡｜回合｜投票｜回顾"
    if is_admin:
        topics += "｜管理"
    return f"{lead}\n\n{topics}"
