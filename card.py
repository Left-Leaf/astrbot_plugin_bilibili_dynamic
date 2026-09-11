"""分享图渲染（Pillow 自绘）。

按 B 站动态卡片的结构画一张分享图，但**只保留卡片上半部分**：头像 + 昵称 + 时间、
完整正文（不折叠）、全部图片；视频/直播等画封面 + 标题。下方的「相关游戏」、
转发/点赞/评论等一律不画。

内嵌转发的动态画成带左侧色条的引用块，块内按同样标准继续画（头像 / 昵称 / 时间 /
正文 / 图片），最多 ``MAX_FORWARD_DEPTH`` 层。

整张图完全本地绘制：不访问任何网页，也不依赖第三方文转图服务。

坐标与字号都用「逻辑像素」书写，内部按 ``SCALE`` 放大绘制，输出清晰度更高。
"""

from __future__ import annotations

import functools
import io
import urllib.request
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont, ImageOps

SCALE = 2
"""绘制倍率：逻辑像素 → 实际像素。"""

WIDTH = 720
"""分享图逻辑宽度。"""

PADDING = 24
"""卡片内边距。"""

GAP = 4
"""图片网格间距（越小图片越大）。"""

RADIUS = 8
"""图片圆角半径（逻辑像素）。"""

MAX_FORWARD_DEPTH = 3
"""内嵌转发的最大绘制层数（防接口异常导致的自我嵌套）。"""


class _Theme:
    """配色（浅色卡片，主色沿用 B 站的粉色）。"""

    bg = (255, 255, 255)
    text = (31, 35, 41)
    sub = (134, 144, 156)
    name = (251, 114, 153)
    quote_bg = (247, 248, 250)
    quote_bar = (251, 114, 153)


FONT_CANDIDATES = {
    "regular": (
        "C:/Windows/Fonts/msyh.ttc",
        "/System/Library/Fonts/PingFang.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    ),
    "bold": (
        "C:/Windows/Fonts/msyhbd.ttc",
        "/System/Library/Fonts/PingFang.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    ),
    "emoji": (
        "C:/Windows/Fonts/seguiemj.ttf",
        "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
        "/System/Library/Fonts/Apple Color Emoji.ttc",
    ),
}
"""字体候选路径（按平台取第一个存在的；都找不到时退化为 Pillow 默认字体）。"""

MEDIA_LABELS = {
    "video": "▶️ 视频",
    "live": "🔴 直播",
    "pgc": "📺 番剧",
    "courses": "🎓 课程",
    "article": "📄 专栏",
}
"""媒体类型对应的前缀标签。"""


def _first_existing(paths: tuple[str, ...]) -> str:
    """返回候选路径中第一个存在的字体文件。

    Args:
        paths: 候选字体路径。

    Returns:
        存在的路径；都不存在时返回空串。
    """
    for candidate in paths:
        if Path(candidate).exists():
            return candidate
    return ""


@functools.lru_cache(maxsize=32)
def _font(kind: str, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """按「字重 + 字号」取字体（带缓存）。

    Args:
        kind: ``regular`` / ``bold`` / ``emoji``。
        size: 逻辑字号。

    Returns:
        字体对象；找不到字体文件时返回 Pillow 默认位图字体。
    """
    path = _first_existing(FONT_CANDIDATES.get(kind, ()))
    if not path:
        return ImageFont.load_default()
    try:
        return ImageFont.truetype(path, int(size * SCALE))
    except OSError:
        return ImageFont.load_default()


NOTDEF_PROBE = "\U0010ffff"
"""用于取「缺字占位字形」的非法码位（各字体都会回退到 .notdef）。"""


@functools.lru_cache(maxsize=8192)
def _glyph_bitmap(
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    char: str,
) -> bytes:
    """把单个字符画到小画布上并返回位图（用于字形存在性判断）。

    Args:
        font: 字体对象。
        char: 单个字符。

    Returns:
        灰度位图字节。
    """
    size = int(getattr(font, "size", 16))
    canvas = Image.new("L", (size * 2 + 8, size * 3 + 8), 0)
    try:
        ImageDraw.Draw(canvas).text((4, 4), char, font=font, fill=255)
    except Exception:  # noqa: BLE001 - 默认位图字体可能不支持
        return b""
    return canvas.tobytes()


def _has_glyph(font: ImageFont.FreeTypeFont | ImageFont.ImageFont, char: str) -> bool:
    """判断字体里是否有该字符的字形。

    注意：不能只看「位图非空」——微软雅黑等字体的缺字占位字形本身就是个方框，
    因此直接与 ``.notdef`` 的位图比较。

    Args:
        font: 字体对象。
        char: 单个字符。

    Returns:
        有真实字形返回 True。
    """
    bitmap = _glyph_bitmap(font, char)
    if not bitmap:
        return True
    return bitmap != _glyph_bitmap(font, NOTDEF_PROBE)


@functools.lru_cache(maxsize=4096)
def _char_font(
    char: str,
    size: int,
    bold: bool,
) -> tuple[ImageFont.FreeTypeFont | ImageFont.ImageFont, bool]:
    """给单个字符挑字体：中文字体有字形就用它，否则回退 emoji 字体（彩色）。

    中文字体（微软雅黑 / 苹方 / Noto CJK）自带大量符号（☆ ★ ■ ◆ ※ → ① …），
    优先用它才不会把 ★ 这类字符画成方框；只有它真缺字时才用 emoji 字体。

    Args:
        char: 单个字符。
        size: 逻辑字号。
        bold: 是否加粗。

    Returns:
        ``(字体, 是否 emoji 字体)``。
    """
    text_font = _font("bold" if bold else "regular", size)
    if char.isspace() or _has_glyph(text_font, char):
        return text_font, False
    return _font("emoji", size), True


@functools.lru_cache(maxsize=8192)
def _advance(char: str, size: int, bold: bool) -> float:
    """测量单个字符的宽度（逻辑像素）。

    Args:
        char: 单个字符。
        size: 逻辑字号。
        bold: 是否加粗。

    Returns:
        宽度。
    """
    font, _ = _char_font(char, size, bold)
    try:
        width = font.getlength(char)
    except Exception:  # noqa: BLE001 - 默认位图字体可能不支持测量
        return size
    return (width / SCALE) if width else size


def _wrap(
    text: str,
    max_width: float,
    size: int,
    bold: bool = False,
) -> list[str]:
    """按宽度折行（英数/链接尽量在空格处断开，CJK 逐字折行）。

    Args:
        text: 原始文本（可含换行）。
        max_width: 行宽上限（逻辑像素）。
        size: 逻辑字号。
        bold: 是否加粗。

    Returns:
        行文本列表（emoji 在绘制时按字符自动切换字体）。
    """
    lines: list[str] = []
    for paragraph in text.split("\n"):
        stripped = paragraph.rstrip()
        if not stripped.strip():
            lines.append("")
            continue
        current = ""
        used = 0.0
        for char in stripped:
            if char in "\ufe0f\u200d":
                continue  # 变体选择符 / 零宽连接符对位图绘制没有帮助
            step = _advance(char, size, bold)
            if used + step > max_width and current:
                # 英数/URL 尽量在空格处断开，避免把链接切得七零八落
                if char.isascii() and " " in current:
                    cut = current.rfind(" ")
                    lines.append(current[:cut])
                    current = current[cut + 1 :]
                    used = sum(_advance(item, size, bold) for item in current)
                else:
                    lines.append(current)
                    current, used = "", 0.0
            current += char
            used += step
        lines.append(current)
    return lines


def _fetch_image(url: str, timeout: float = 12.0) -> Image.Image | None:
    """下载图片（带 Referer，B 站 CDN 需要）。

    Args:
        url: 图片地址。
        timeout: 超时秒数。

    Returns:
        RGB 图片；下载或解码失败返回 None。
    """
    if not url:
        return None
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - 地址来自 B 站接口
            data = response.read()
        return Image.open(io.BytesIO(data)).convert("RGB")
    except Exception:  # noqa: BLE001 - 单张图失败不影响整张卡片
        return None


class _Painter:
    """绘制上下文：``image=None`` 时只推进光标（测量模式），用于先算高度再真正绘制。"""

    def __init__(self, image: Image.Image | None = None) -> None:
        self.image = image
        self.draw = ImageDraw.Draw(image) if image is not None else None

    def avatar(self, x: float, y: float, size: float, pil: Image.Image) -> None:
        """画圆形头像。

        Args:
            x: 左边界。
            y: 顶边。
            size: 直径。
            pil: 头像原图。
        """
        if self.image is None:
            return
        pixels = max(1, int(size * SCALE))
        mask = Image.new("L", (pixels, pixels), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, pixels, pixels), fill=255)
        self.image.paste(
            ImageOps.fit(pil, (pixels, pixels), method=Image.Resampling.LANCZOS),
            (int(x * SCALE), int(y * SCALE)),
            mask,
        )

    def text_lines(
        self,
        x: float,
        y: float,
        lines: list[list[tuple[str, bool]]],
        size: int,
        color: tuple[int, int, int],
        bold: bool = False,
        line_ratio: float = 1.55,
    ) -> float:
        """绘制多行文本（含 emoji）。

        Args:
            x: 左边界。
            y: 顶边。
            lines: ``_wrap`` 的结果。
            size: 逻辑字号。
            color: 文字颜色。
            bold: 是否加粗。
            line_ratio: 行高倍率。

        Returns:
            绘制后的新 y。
        """
        line_height = size * line_ratio
        for line in lines:
            if self.draw is not None and line:
                cursor = x
                for char in line:
                    font, is_emoji = _char_font(char, size, bold)
                    try:
                        self.draw.text(
                            (cursor * SCALE, y * SCALE),
                            char,
                            font=font,
                            fill=color,
                            embedded_color=is_emoji,
                        )
                    except Exception:  # noqa: BLE001 - 字体不支持彩色 emoji 时退回单色
                        self.draw.text(
                            (cursor * SCALE, y * SCALE), char, font=font, fill=color
                        )
                    cursor += _advance(char, size, bold)
            y += line_height
        return y

    def image_box(
        self,
        x: float,
        y: float,
        width: float,
        height: float,
        pil: Image.Image,
        radius: float = RADIUS,
    ) -> float:
        """把图片按 cover 裁切、带圆角地贴进指定方框。

        Args:
            x: 左边界。
            y: 顶边。
            width: 方框宽。
            height: 方框高。
            pil: 源图片。
            radius: 圆角半径。

        Returns:
            绘制后的新 y。
        """
        if self.image is not None:
            pixels_w = max(1, int(width * SCALE))
            pixels_h = max(1, int(height * SCALE))
            target = ImageOps.fit(
                pil, (pixels_w, pixels_h), method=Image.Resampling.LANCZOS
            )
            mask = Image.new("L", (pixels_w, pixels_h), 0)
            ImageDraw.Draw(mask).rounded_rectangle(
                (0, 0, pixels_w - 1, pixels_h - 1),
                radius=radius * SCALE,
                fill=255,
            )
            self.image.paste(target, (int(x * SCALE), int(y * SCALE)), mask)
        return y + height

    def rounded_rect(
        self,
        x: float,
        y: float,
        width: float,
        height: float,
        fill: tuple[int, int, int] | None = None,
        radius: float = 12,
    ) -> None:
        """画圆角矩形（引用块底色、色条）。

        Args:
            x: 左边界。
            y: 顶边。
            width: 宽度。
            height: 高度。
            fill: 填充色。
            radius: 圆角半径。
        """
        if self.draw is None or fill is None:
            return
        self.draw.rounded_rectangle(
            (x * SCALE, y * SCALE, (x + width) * SCALE, (y + height) * SCALE),
            radius=radius * SCALE,
            fill=fill,
        )


def _collect_urls(dynamic: dict, depth: int = 0) -> list[str]:
    """收集一条动态（含内嵌体）需要用到的所有图片地址。

    Args:
        dynamic: 归一化后的动态。
        depth: 当前层级。

    Returns:
        去重后的图片地址列表。
    """
    urls: list[str] = []

    def push(url: Any) -> None:
        text = str(url or "").strip()
        if text and text not in urls:
            urls.append(text)

    push(dynamic.get("avatar"))
    media = dynamic.get("video")
    if isinstance(media, dict):
        push(media.get("cover"))
    else:
        for url in dynamic.get("images") or []:
            push(url)
    nested = dynamic.get("forward")
    if isinstance(nested, dict) and depth < MAX_FORWARD_DEPTH:
        for url in _collect_urls(nested, depth + 1):
            push(url)
    return urls


def _draw_content(
    painter: _Painter,
    dynamic: dict,
    images: dict[str, Image.Image],
    x: float,
    y: float,
    width: float,
    nested: bool,
) -> float:
    """绘制动态的内容区：图文画全部图片，视频/直播等画封面 + 标题 + 链接。

    Args:
        painter: 绘制上下文。
        dynamic: 归一化后的动态。
        images: 已下载的图片（URL → PIL 图片）。
        x: 内容左边界。
        y: 内容顶边。
        width: 内容宽度。
        nested: 是否为内嵌体（字号略小）。

    Returns:
        绘制后的新 y。
    """
    media = dynamic.get("video")
    if isinstance(media, dict):
        cover = images.get(str(media.get("cover") or ""))
        if cover is not None:
            height = min(width * 0.62, width * cover.height / max(cover.width, 1))
            y = painter.image_box(x, y, width, height, cover)
            y += 8
        title = str(media.get("title") or "").strip()
        if title:
            y = painter.text_lines(
                x, y, _wrap(title, width, 15, True), 15, _Theme.text, bold=True
            )
        url = str(media.get("url") or "").strip()
        if url:
            label = MEDIA_LABELS.get(str(media.get("kind") or ""), "▶️ 视频")
            y = painter.text_lines(
                x,
                y,
                _wrap(f"{label} {url}", width, 13),
                13,
                _Theme.name,
            )
        return y

    urls = [str(url) for url in dynamic.get("images") or []]
    available = [images[url] for url in urls if url in images]
    if not available:
        return y

    if len(available) == 1:
        cover = available[0]
        # 单图按原比例完整展示（限制最大高度，避免长图把卡片拉得过长）
        height = min(width * 1.4, width * cover.height / max(cover.width, 1))
        return painter.image_box(x, y, width, height, cover) + 10

    # 列数按 B 站习惯分配：2 图两列、3 图三列、4 图两列（2x2）、5-9 图三列
    if len(available) <= 3:
        columns = len(available)
    elif len(available) == 4:
        columns = 2
    else:
        columns = 3
    cell = (width - GAP * (columns - 1)) / columns
    for index, cover in enumerate(available):
        row, column = divmod(index, columns)
        painter.image_box(
            x + column * (cell + GAP),
            y + row * (cell + GAP),
            cell,
            cell,
            cover,
        )
    rows = (len(available) + columns - 1) // columns
    return y + rows * cell + (rows - 1) * GAP + 10


def _draw_block(
    painter: _Painter,
    dynamic: dict,
    images: dict[str, Image.Image],
    x: float,
    y: float,
    width: float,
    nested: bool,
    depth: int,
) -> float:
    """绘制一条动态**自身**的内容（头部 + 正文 + 图片，不含引用块底色）。

    Args:
        painter: 绘制上下文。
        dynamic: 归一化后的动态。
        images: 已下载的图片。
        x: 左边界。
        y: 顶边。
        width: 可用宽度。
        nested: 是否为内嵌体（字号略小）。
        depth: 当前层级。

    Returns:
        绘制后的新 y。
    """
    if nested:
        avatar_size, name_size, time_size, text_size = 26, 14, 11, 14
    else:
        avatar_size, name_size, time_size, text_size = 44, 17, 12, 15

    avatar = images.get(str(dynamic.get("avatar") or ""))
    if avatar is not None:
        painter.avatar(x, y, avatar_size, avatar)

    name_x = x + (avatar_size + 12 if avatar is not None else 0)
    name_width = width - (avatar_size + 12 if avatar is not None else 0)
    name_y = painter.text_lines(
        name_x,
        y + (1 if nested else 2),
        _wrap(str(dynamic.get("author") or "未知 UP 主"), name_width, name_size, True),
        name_size,
        _Theme.name,
        bold=True,
    )
    pub_time = str(dynamic.get("pubTimeText") or "").strip()
    if pub_time:
        painter.text_lines(name_x, name_y - 2, [pub_time], time_size, _Theme.sub)
    y = max(name_y + 12, y + avatar_size + 12)

    text = str(dynamic.get("text") or "").strip()
    if text:
        y = painter.text_lines(
            x, y, _wrap(text, width, text_size), text_size, _Theme.text
        )
        y += 8

    y = _draw_content(painter, dynamic, images, x, y, width, nested)

    child = dynamic.get("forward")
    if isinstance(child, dict) and depth < MAX_FORWARD_DEPTH:
        y += 8
        y = _draw_dynamic(
            painter, child, images, x, y, width, nested=True, depth=depth + 1
        )
    return y


def _draw_dynamic(
    painter: _Painter,
    dynamic: dict,
    images: dict[str, Image.Image],
    x: float,
    y: float,
    width: float,
    nested: bool = False,
    depth: int = 0,
) -> float:
    """绘制一条动态（内嵌体先铺底色再画内容），返回新的 y。

    Args:
        painter: 绘制上下文。
        dynamic: 归一化后的动态。
        images: 已下载的图片。
        x: 左边界。
        y: 顶边。
        width: 可用宽度。
        nested: 是否为内嵌体（引用块样式）。
        depth: 当前层级。

    Returns:
        绘制后的新 y。
    """
    if not nested:
        return _draw_block(
            painter, dynamic, images, x, y, width, nested=False, depth=depth
        )

    # 引用块：先量出自身高度 → 铺底色与色条 → 再画内容（保证底色在内容下方）
    padding = 12
    inner_x = x + padding + 8
    inner_width = width - padding * 2 - 8
    top = y + padding
    bottom = _draw_block(
        _Painter(None),
        dynamic,
        images,
        inner_x,
        top,
        inner_width,
        nested=True,
        depth=depth,
    )
    height = bottom - y + padding
    painter.rounded_rect(x, y, width, height, fill=_Theme.quote_bg)
    painter.rounded_rect(x, y, 3, height, fill=_Theme.quote_bar, radius=2)
    return (
        _draw_block(
            painter,
            dynamic,
            images,
            inner_x,
            top,
            inner_width,
            nested=True,
            depth=depth,
        )
        + padding
    )


def render_share_card(dynamic: dict, out_path: Path) -> Path:
    """把一条动态画成分享图并保存。

    Args:
        dynamic: 归一化后的动态数据（``node/dynamics.ts`` 的输出）。
        out_path: 输出 PNG 路径。

    Returns:
        输出路径。

    Raises:
        OSError: 图片写入失败。
    """
    images: dict[str, Image.Image] = {}
    for url in _collect_urls(dynamic):
        pil = _fetch_image(url)
        if pil is not None:
            images[url] = pil

    content_width = WIDTH - PADDING * 2
    measure = _Painter(None)
    height = _draw_dynamic(measure, dynamic, images, PADDING, PADDING, content_width)

    canvas = Image.new(
        "RGB",
        (WIDTH * SCALE, max(int(height + PADDING), PADDING * 2) * SCALE),
        _Theme.bg,
    )
    painter = _Painter(canvas)
    drawn = _draw_dynamic(painter, dynamic, images, PADDING, PADDING, content_width)
    # 按实际画到的高度裁剪（测量与绘制有任何细小偏差都不会留下多余留白）
    bottom = min(int(drawn + PADDING), canvas.height // SCALE)
    canvas = canvas.crop((0, 0, WIDTH * SCALE, max(bottom, PADDING * 2) * SCALE))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path, "PNG")
    return out_path
