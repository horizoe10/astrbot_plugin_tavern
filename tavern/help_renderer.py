"""把开团指令帮助渲染成 PNG 图片（私聊 / 群聊两套）。

用 PIL + MiSans（容器里已有的中文字体）生成。设计原则：
- 私聊精简版只覆盖建卡流程；群聊全指令清单。
- 长文本用自动换行；超长条目按语法 / 强分隔符拆行。
- 配色：浅色卡片背景（QQ 私聊/群聊都适用），深色文字。
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from PIL import Image, ImageDraw, ImageFont


_FONT_CANDIDATES: Final[tuple[str, ...]] = (
    "/AstrBot/data/steam_status_monitor/fonts/MiSans-Regular.ttf",
    "/AstrBot/data/steam_status_monitor/fonts/NotoSansHans-Regular.otf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)


def _resolve_font_path() -> str:
    """Return the first available CJK-capable font."""

    for candidate in _FONT_CANDIDATES:
        if os.path.exists(candidate):
            return candidate
    raise FileNotFoundError(
        "未找到可用的中文字体，已尝试：" + ", ".join(_FONT_CANDIDATES)
    )


@dataclass(frozen=True)
class HelpSection:
    """帮助图的一节：标题 + 命令/说明列表。"""

    title: str
    items: tuple[str, ...]


@dataclass(frozen=True)
class HelpPalette:
    background: tuple[int, int, int] = (248, 248, 252)
    card: tuple[int, int, int] = (255, 255, 255)
    border: tuple[int, int, int] = (228, 230, 236)
    title: tuple[int, int, int] = (32, 36, 48)
    section: tuple[int, int, int] = (38, 99, 235)
    text: tuple[int, int, int] = (40, 44, 56)
    muted: tuple[int, int, int] = (110, 116, 132)
    accent: tuple[int, int, int] = (236, 72, 153)


def _load_font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


def _measure(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> tuple[int, int]:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def _wrap_line(
    text: str,
    max_width: int,
    measure_fn,
) -> list[str]:
    """把一行拆成多行，每行宽度不超过 max_width。"""

    if not text:
        return [""]
    # 已经手工换行过的部分保留
    raw_lines = text.split("\n")
    wrapped: list[str] = []
    for raw in raw_lines:
        if not raw:
            wrapped.append("")
            continue
        # 按强分隔符（> → ｜ 空格，标点）拆开更整齐
        tokens: list[str] = []
        buf = ""
        for char in raw:
            if char in "｜|→→" or (
                char in " 　" and buf and not buf.endswith(("｜", "|", "→"))
            ):
                if buf:
                    tokens.append(buf)
                buf = ""
            buf += char
        if buf:
            tokens.append(buf)
        current = ""
        for token in tokens:
            candidate = (current + token) if current else token
            if measure_fn(candidate)[0] <= max_width:
                current = candidate
                continue
            if current:
                wrapped.append(current)
            # 单 token 自身就超长，硬切字符
            while measure_fn(token)[0] > max_width:
                cut = 1
                while (
                    cut < len(token)
                    and measure_fn(token[:cut])[0] <= max_width
                ):
                    cut += 1
                wrapped.append(token[: max(cut - 1, 1)])
                token = token[max(cut - 1, 1):]
            current = token
        if current:
            wrapped.append(current)
    return wrapped


# 私聊建卡指令（精简版）
PRIVATE_HELP_SECTIONS: Final[tuple[HelpSection, ...]] = (
    HelpSection(
        "📌 私聊建卡流程",
        (
            "/团 当前          查看当前步骤和候选",
            "/团 下一批       继续发送当前字段的候选",
            "/团 查看选项 <序号>   看某个候选的详情",
            "/团 预览          查看已填资料",
        ),
    ),
    HelpSection(
        "✏️ 填写与修改",
        (
            "/团 修改 <完整字段名称>   重新填写某个字段",
            "/团 修改角色名 <新名称>  改名",
            "/团 修改昵称 <新昵称>   改副本昵称",
            "/团 上一步       返回上一步",
            "/团 重填数值     重新随机属性",
        ),
    ),
    HelpSection(
        "🤖 AI 设定助手（私聊建卡中可用）",
        (
            "/团 随机          让模型代写当前字段",
            "/团 补全 <初始设定>    把你的想法扩写成完整设定",
            "→ 选择题字段不需要 补全；随机不消耗 AI 次数",
        ),
    ),
    HelpSection(
        "🌐 网页建卡",
        (
            "/团 网页建卡    私聊签发 15 分钟链接",
            "→ 手机/电脑浏览器逐项填写，含 AI 按钮",
            "→ 网页激活期间聊天侧不再推送候选",
        ),
    ),
    HelpSection(
        "✅ 完成或撤销",
        (
            "/团 确认建卡     提交角色卡进入审核",
            "/团 取消建卡     撤销草稿，保留席位",
            "/团 重新建卡     重新开始建卡",
            "/团 放弃席位 确认  释放当前副本席位",
        ),
    ),
    HelpSection(
        "❓ 进阶查询",
        (
            "/团 帮助 建卡     这一张图",
            "→ /团 帮助 建卡｜回合｜投票｜战术｜回顾｜管理",
        ),
    ),
)


# 群聊全指令（按 HELP_TEXT_TEMPLATE 重组为分节版）
def _parse_group_sections(trigger_prefix: str) -> tuple[HelpSection, ...]:
    p = trigger_prefix.strip() or "t"
    return (
        HelpSection(
            "🎬 主持",
            (
                f"/团 开启 <副本>    {p} 跟随触发器",
                "/团 开演          正式开始故事",
                "/团 状态          查看副本状态",
            ),
        ),
        HelpSection(
            "⏸ 暂停 / 恢复",
            (
                "/团 暂停 → /团 恢复 → 全员准备 → /团 继续",
            ),
        ),
        HelpSection(
            "👥 玩家",
            (
                "/团 加入｜角色｜准备｜阵容",
                "/团 暂离｜返回队列｜退出",
            ),
        ),
        HelpSection(
            "🃏 建卡（先在群内 /团 加入 取得席位，再私聊机器人）",
            (
                "私聊：/团 建卡 <验证码>｜当前｜上一步｜修改 <字段>",
                "私聊：/团 预览｜重填数值｜确认建卡｜取消建卡",
            ),
        ),
        HelpSection(
            "🤖 AI 设定助手（私聊建卡中可用）",
            (
                "/团 随机           让模型代写当前字段",
                "/团 补全 <初始设定>     把你的想法扩写成完整设定",
            ),
        ),
        HelpSection(
            "🌐 网页建卡（私聊签发链接，浏览器逐项填写）",
            (
                "私聊：/团 网页建卡",
            ),
        ),
        HelpSection(
            "✏️ 改名 / 草稿",
            (
                "/团 修改角色名 <名称>｜/团 修改昵称 <昵称>",
                "/团 重新建卡｜取消建卡｜放弃席位 确认",
            ),
        ),
        HelpSection(
            "🎯 回合",
            (
                f"{p} A           选 A",
                "/团 选择 A｜/团 重整选项",
            ),
        ),
        HelpSection(
            "🎒 物资 / 商店",
            (
                f"{p} 道具 <名称>｜{p} 技能 <名称>",
                "/团 赠予 <道具> <目标>",
                "/团 商店｜/团 购买 <商品>",
            ),
        ),
        HelpSection(
            "⚖️ 裁定 / 集体 / 命运",
            (
                "/团 灵感｜/团 灵感 A 优势｜/团 灵感重投 A",
                "/团 投票 A（不消耗个人行动）",
                "/团 命运预览｜/团 命运确认 <编号>｜/团 命运拒绝 <编号>",
                "/团 救援 <角色完整名称或副本昵称>",
            ),
        ),
        HelpSection(
            "💾 记录",
            (
                "/团 回顾｜存档列表｜存档 <名称>｜删档 <名称>",
                "/团 读档｜回滚",
            ),
        ),
        HelpSection(
            "🛡 管理（仅管理员）",
            (
                "审核｜强制全员准备｜AI队友 <数量> <确认|自动|暂停>",
                "倒计时｜用量｜限额｜移至｜指定",
            ),
        ),
        HelpSection(
            "⚔️ 战术",
            (
                "战况｜行动/防守/援助/撤退/谈判",
                "锁定行动｜推进战术｜纠正战术｜结束战术",
            ),
        ),
        HelpSection(
            "🧩 挑战",
            (
                "挑战｜挑战行动｜退出挑战｜挑战谈判",
                "确认挑战｜推进挑战｜结束挑战",
            ),
        ),
        HelpSection(
            "🎙 主持（详细）",
            (
                "/团 主持 开启｜指引｜推进｜直述",
                "/团 主持 交棒｜自动｜状态｜接管",
            ),
        ),
        HelpSection(
            "🆘 安全 / 收尾",
            (
                "任一出场玩家可发 /团 安全暂停",
                "/团 关闭｜/团 完结 确认｜/团 强制终止 确认 <原因>",
            ),
        ),
        HelpSection(
            "❓ 子帮助",
            (
                "/团 帮助 建卡｜回合｜投票｜战术｜回顾｜管理",
            ),
        ),
    )


# 群聊 help 时返回的动态 section
def build_group_sections(trigger_prefix: str) -> tuple[HelpSection, ...]:
    return _parse_group_sections(trigger_prefix)


def render_help_image(
    *,
    scope: str,
    sections: Sequence[HelpSection],
    output_path: str,
    title: str = "321开团 帮助",
    subtitle: str = "",
    palette: HelpPalette | None = None,
    font_path: str | None = None,
) -> Path:
    """把帮助 section 列表渲染成 PNG，写到 output_path。

    ``scope`` 仅用于标题区分（"group" / "private"），不影响布局。
    """

    pal = palette or HelpPalette()
    font_path = font_path or _resolve_font_path()
    title_font = _load_font(font_path, 28)
    section_font = _load_font(font_path, 22)
    item_font = _load_font(font_path, 20)
    muted_font = _load_font(font_path, 16)

    # 预渲染一张临时画布，仅用于测量
    tmp = Image.new("RGB", (10, 10), pal.background)
    draw = ImageDraw.Draw(tmp)

    canvas_width = 920
    margin_x = 32
    line_gap = 8
    section_gap = 20

    # 计算所有行（已经按宽度自动换行）
    laid_out: list[tuple[str, str, int, int]] = []
    # (kind, text, width, height)
    # kind ∈ {"title", "subtitle", "section", "item", "muted"}
    for sec in sections:
        for item in sec.items:
            lines = _wrap_line(item, canvas_width - margin_x * 2, lambda t: _measure(draw, t, item_font))
            for line in lines:
                w, h = _measure(draw, line, item_font)
                laid_out.append(("item", line, w, h))
    title_w, title_h = _measure(draw, title, title_font)
    laid_out.insert(0, ("title", title, title_w, title_h))
    if subtitle:
        sub_w, sub_h = _measure(draw, subtitle, muted_font)
        laid_out.insert(1, ("subtitle", subtitle, sub_w, sub_h))

    # 重新计算真实高度
    body_lines: list[tuple[str, str, ImageFont.ImageFont, int]] = []
    body_lines.append(("title", title, title_font, line_gap * 3))
    if subtitle:
        body_lines.append(("subtitle", subtitle, muted_font, line_gap))
    body_lines.append(("spacer", "", muted_font, section_gap))
    for sec_idx, sec in enumerate(sections):
        body_lines.append(("section", sec.title, section_font, section_gap))
        for item in sec.items:
            lines = _wrap_line(item, canvas_width - margin_x * 2, lambda t: _measure(draw, t, item_font))
            for line in lines:
                body_lines.append(("item", line, item_font, line_gap))

    # 计算高度
    total_h = margin_x * 2
    for kind, text, font, gap in body_lines:
        if kind == "spacer":
            total_h += gap
            continue
        _, h = _measure(draw, text, font)
        total_h += h + gap

    canvas_height = max(total_h, 400)
    img = Image.new("RGB", (canvas_width, canvas_height), pal.background)
    draw = ImageDraw.Draw(img)

    y = margin_x
    for kind, text, font, gap in body_lines:
        if kind == "spacer":
            y += gap
            continue
        _, h = _measure(draw, text, font)
        if kind == "title":
            draw.text((margin_x, y), text, font=font, fill=pal.title)
        elif kind == "subtitle":
            draw.text((margin_x, y), text, font=font, fill=pal.muted)
        elif kind == "section":
            # 章节标题：粉色重音 + 圆点
            dot_w, _ = _measure(draw, "●  ", font)
            draw.text((margin_x, y), "●", font=font, fill=pal.accent)
            draw.text((margin_x + dot_w, y), text, font=font, fill=pal.section)
        elif kind == "item":
            # 命令用等宽对齐：左 8px 缩进
            draw.text((margin_x + 12, y), text, font=font, fill=pal.text)
        y += h + gap

    # 在画布四周画一个浅边框
    draw.rectangle(
        [(0, 0), (canvas_width - 1, canvas_height - 1)],
        outline=pal.border,
        width=1,
    )

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, format="PNG", optimize=True)
    return out


def render_group_help_image(
    *,
    output_dir: str,
    trigger_prefix: str,
    plugin_version: str,
    palette: HelpPalette | None = None,
    font_path: str | None = None,
) -> Path:
    sections = build_group_sections(trigger_prefix)
    stamp = int(time.time())
    path = Path(output_dir) / f"tavern_help_group_{stamp}.png"
    return render_help_image(
        scope="group",
        sections=sections,
        output_path=str(path),
        title=f"321开团 帮助 · 群聊  v{plugin_version}",
        subtitle="触发前缀：" + (trigger_prefix.strip() or "t"),
        palette=palette,
        font_path=font_path,
    )


def render_private_help_image(
    *,
    output_dir: str,
    plugin_version: str,
    palette: HelpPalette | None = None,
    font_path: str | None = None,
) -> Path:
    stamp = int(time.time())
    path = Path(output_dir) / f"tavern_help_private_{stamp}.png"
    return render_help_image(
        scope="private",
        sections=PRIVATE_HELP_SECTIONS,
        output_path=str(path),
        title=f"321开团 帮助 · 私聊建卡  v{plugin_version}",
        subtitle="与 Bot 私聊时可用，先在群内 /团 加入 取得席位与建卡码",
        palette=palette,
        font_path=font_path,
    )
