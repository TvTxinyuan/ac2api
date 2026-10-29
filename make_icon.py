"""生成应用图标 assets/ac2api.ico（多尺寸）。

单独写个脚本而不是把二进制塞进仓库：图标是「画出来的」，源码可读、可改，
改颜色或形状重跑一次即可。

    python make_icon.py
"""
from __future__ import annotations

import os

from PIL import Image, ImageDraw

GREEN = (46, 204, 160)
DARK = (16, 40, 36)
WHITE = (255, 255, 255)


def draw(size: int) -> Image.Image:
    """画一张 size×size 的图标：圆角方块 + 白色「>」提示符。"""
    # 4 倍超采样再缩，边缘才不会有锯齿。
    s = size * 4
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    radius = int(s * 0.22)
    d.rounded_rectangle((0, 0, s - 1, s - 1), radius=radius, fill=GREEN)
    # 内层深色圆角，让图标有厚度，小尺寸下也不糊成一块。
    inset = int(s * 0.07)
    d.rounded_rectangle(
        (inset, inset, s - 1 - inset, s - 1 - inset),
        radius=int(radius * 0.82),
        fill=DARK,
    )

    # 「>」提示符：两笔线段凑出来，比字体渲染可控。
    lw = max(2, int(s * 0.075))
    x0, y0 = s * 0.31, s * 0.28
    x1, y1 = s * 0.53, s * 0.50
    x2, y2 = s * 0.31, s * 0.72
    d.line((x0, y0, x1, y1), fill=WHITE, width=lw, joint="curve")
    d.line((x1, y1, x2, y2), fill=WHITE, width=lw, joint="curve")

    # 右下角一个小方块，暗示「积分/令牌」。
    bw = int(s * 0.13)
    d.rounded_rectangle(
        (s * 0.60, s * 0.60, s * 0.60 + bw * 1.6, s * 0.60 + bw),
        radius=int(bw * 0.3),
        fill=GREEN,
    )
    return img.resize((size, size), Image.LANCZOS)


def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    assets = os.path.join(here, "assets")
    os.makedirs(assets, exist_ok=True)

    sizes = [16, 20, 24, 32, 40, 48, 64, 128, 256]
    images = [draw(n) for n in sizes]
    ico = os.path.join(assets, "ac2api.ico")
    # 最大的当主图，其余作为 ICO 里的多尺寸帧。
    images[-1].save(ico, format="ICO",
                    sizes=[(n, n) for n in sizes])
    png = os.path.join(assets, "ac2api.png")
    images[-1].save(png, format="PNG")
    print(f"已生成 {ico}（{os.path.getsize(ico)} 字节）")
    print(f"已生成 {png}（{os.path.getsize(png)} 字节）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
