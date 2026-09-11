"""动态处理模块：把归一化后的动态拼成待发送的消息链。

归一化数据由 ``node/dynamics.ts`` 产出（``author`` / ``text`` / ``images`` /
``video`` / ``forward`` …）。本模块提供四种分发样式：

* ``auto``（默认）：**自绘分享图**——由 ``card.render_share_card`` 用 Pillow 把动态
  画成一张图（UP 名 + 正文 + 全部图片；视频类只画封面与链接；内嵌转发画成引用块），
  发送时只发这张图；渲染失败自动退回 ``rich``。
* ``rich``：一条消息里依次排开「文字块 + 该块的全部图片」，内嵌转发同样按此标准排在后面。
* ``text``：只发文字（含内嵌体的文字与链接，不带图）。
* ``forward``：QQ 合并转发（节点昵称 = UP 主）。

图片直发与合并转发只有 QQ（aiocqhttp / OneBot v11）支持，其它平台统一降级为文字。
"""

from __future__ import annotations

import time

from astrbot.api.event import MessageChain
from astrbot.api.message_components import Image, Node, Nodes, Plain

QQ_PLATFORM_NAME = "aiocqhttp"
"""支持图片直发与合并转发的平台适配器类型（OneBot v11）。"""

STYLE_AUTO = "auto"
STYLE_RICH = "rich"
STYLE_TEXT = "text"
STYLE_FORWARD = "forward"

MEDIA_LABELS = {
    "video": "▶️ 视频",
    "live": "🔴 直播",
    "pgc": "📺 番剧",
    "courses": "🎓 课程",
    "article": "📄 专栏",
}
"""``video`` 字段里 ``kind`` 对应的展示标签。"""

MAX_FORWARD_DEPTH = 3
"""内嵌转发的最大层数（正常只有一层，这里只做防御）。"""


def _block_text(dynamic: dict, nested: bool = False) -> str:
    """生成一条动态的文本块（不含它的内嵌体）。

    Args:
        dynamic: 归一化后的动态数据。
        nested: 是否为内嵌转发的那条（用「转发自」标题，便于区分层级）。

    Returns:
        多行文本，包含作者、正文（或媒体标题）、时间、动态链接与媒体链接。
    """
    author = str(dynamic.get("author") or dynamic.get("uid") or "未知 UP 主")
    lines = [f"—— 转发自 @{author} ——" if nested else f"【B站动态】{author}"]

    text = str(dynamic.get("text") or "").strip()
    media = dynamic.get("video")
    media = media if isinstance(media, dict) else {}
    title = str(media.get("title") or "").strip()
    # 视频/直播的正文常常就是标题，避免重复一行
    if text and text != title:
        lines.append(text)
    elif not text and title:
        lines.append(title)

    pub_time = str(dynamic.get("pubTimeText") or "").strip()
    if not pub_time:
        # 内嵌转发拿不到相对时间时，用发布时间戳补一个绝对时间
        pub_ts = int(dynamic.get("pubTs") or 0)
        if pub_ts > 0:
            pub_time = time.strftime("%m-%d %H:%M", time.localtime(pub_ts))
    if pub_time:
        lines.append(f"🕒 {pub_time}")
    link = str(dynamic.get("link") or "").strip()
    if link:
        lines.append(f"🔗 {link}")

    media_url = str(media.get("url") or "").strip()
    if media_url:
        label = MEDIA_LABELS.get(str(media.get("kind") or ""), "▶️ 视频")
        lines.append(f"{label}：{media_url}")
    return "\n".join(lines)


def _image_urls(dynamic: dict) -> list[str]:
    """取出该条动态要发的图片（视频类只发封面，其它发全部图片）。

    Args:
        dynamic: 归一化后的动态数据。

    Returns:
        去重后的图片 URL 列表。
    """
    media = dynamic.get("video")
    if isinstance(media, dict) and str(media.get("cover") or "").strip():
        return [str(media["cover"]).strip()]

    urls: list[str] = []
    images = dynamic.get("images")
    for item in images if isinstance(images, list) else []:
        url = str(item or "").strip()
        if url and url not in urls:
            urls.append(url)
    return urls


def summary_text(dynamic: dict) -> str:
    """生成整条动态的纯文本（含内嵌转发的文本，不含图片）。

    Args:
        dynamic: 归一化后的动态数据。

    Returns:
        多行文本；内嵌体用空行分隔。
    """
    blocks: list[str] = []
    current: dict | None = dynamic
    depth = 0
    while isinstance(current, dict) and depth <= MAX_FORWARD_DEPTH:
        blocks.append(_block_text(current, nested=depth > 0))
        nested = current.get("forward")
        current = nested if isinstance(nested, dict) else None
        depth += 1
    return "\n\n".join(blocks)


def build_dynamic_chain(
    dynamic: dict,
    platform_name: str,
    style: str,
    card_path: str = "",
) -> MessageChain:
    """把一条动态转换成待发送的消息链。

    Args:
        dynamic: 归一化后的动态数据。
        platform_name: 目标会话所属平台适配器类型（``event.get_platform_name()``）。
        style: 分发样式，``auto`` / ``rich`` / ``text`` / ``forward``。
        card_path: ``auto`` 样式下已渲染好的分享图路径（空串表示未渲染，退回 ``rich``）。

    Returns:
        可直接交给 ``context.send_message()`` 的消息链。
    """
    mode = (
        style
        if style in {STYLE_AUTO, STYLE_RICH, STYLE_TEXT, STYLE_FORWARD}
        else STYLE_AUTO
    )

    if platform_name != QQ_PLATFORM_NAME:
        # 图片直发与合并转发只有 QQ 适配器支持，其它平台统一用文本。
        return MessageChain([Plain(summary_text(dynamic))])

    if mode == STYLE_FORWARD:
        author = str(dynamic.get("author") or "未知 UP 主")
        uid = str(dynamic.get("uid") or "0")
        node = Node(content=[Plain(summary_text(dynamic))], name=author, uin=uid or "0")
        return MessageChain([Nodes([node])])

    if mode == STYLE_AUTO and card_path:
        # 分享图 + 一条可点击的动态链接（图片里也画了链接，这里再给一段便于点击/复制）
        segments: list = [Image.fromFileSystem(card_path)]
        link = str(dynamic.get("link") or "").strip()
        if link:
            segments.append(Plain(f"\n🔗 {link}"))
        return MessageChain(segments)

    if mode == STYLE_TEXT:
        return MessageChain([Plain(summary_text(dynamic))])

    # rich：文本块与图片块按层级依次排开（内嵌转发的图文紧跟在它自己的文本块之后）
    segments: list = []
    current: dict | None = dynamic
    depth = 0
    while isinstance(current, dict) and depth <= MAX_FORWARD_DEPTH:
        segments.append(Plain(_block_text(current, nested=depth > 0)))
        for url in _image_urls(current):
            segments.append(Image(file=url))
        nested = current.get("forward")
        current = nested if isinstance(nested, dict) else None
        depth += 1
    return MessageChain(segments)
