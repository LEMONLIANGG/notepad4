"""校验扩展后的工具栏位图。

检查项：
  1. 位图宽度应等于 图标数 x 单图标边长（16/24/32/40/48 分别为 29 个图标）
  2. 原有前 27 个图标的像素必须与原图逐字节一致（未被改动）
  3. 与显示相关的头部字段（位深/压缩/平面数/DPI）必须与原图保持一致
  4. 如安装了 Pillow，额外合成一张图标 24..28 在浅色/深色背景下的预览图
"""

import struct
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SIZES = (16, 24, 32, 40, 48)
ORIG_ICONS = 27          # 上游原始图标数
NEW_ICONS = 29           # 追加「上一个 / 下一个」后的图标数


def bmp(raw):
    """返回 (宽, 高, 头部字节, 像素字节)。像素为自底向上、每像素 4 字节 BGRA。"""
    off = struct.unpack_from('<I', raw, 10)[0]
    w, h = struct.unpack_from('<ii', raw, 18)
    return w, h, raw[:off], raw[off:]


def header_fingerprint(raw):
    """与显示相关的头部字段：平面数、位深、压缩、每米像素数。"""
    planes, bpp, comp = struct.unpack_from('<HHI', raw, 26)
    xppm, yppm = struct.unpack_from('<ii', raw, 38)
    return (planes, bpp, comp, xppm, yppm)


ok = True
for s in SIZES:
    old = subprocess.run(['git', 'show', f'HEAD:res/Toolbar{s}.bmp'],
                         cwd=ROOT, capture_output=True, check=True).stdout
    new = (ROOT / f'res/Toolbar{s}.bmp').read_bytes()
    ow, oh, _, opx = bmp(old)
    nw, nh, _, npx = bmp(new)

    problems = []
    if (ow, oh) != (s * ORIG_ICONS, s):
        problems.append(f'原图尺寸异常 {ow}x{oh}')
    if (nw, nh) != (s * NEW_ICONS, s):
        problems.append(f'新图尺寸异常 {nw}x{nh}')
    if header_fingerprint(old) != header_fingerprint(new):
        problems.append('显示相关头部字段被改动')

    # 逐像素比较前 27 个图标（像素自底向上存储）
    diff = 0
    if (nw, nh) == (s * NEW_ICONS, s):
        for y in range(nh):
            o_row = (nh - 1 - y) * ow * 4
            n_row = (nh - 1 - y) * nw * 4
            for x in range(ow * 4):
                if opx[o_row + x] != npx[n_row + x]:
                    diff += 1
        if diff:
            problems.append(f'原图标有 {diff} 字节差异')

    if problems:
        ok = False
    detail = '原 27 图标字节完全一致' if not diff else f'差异 {diff} 字节'
    print(f'Toolbar{s:<2}: {nw}x{nh} → {nw // s} 个图标 | {detail}'
          + ('' if not problems else '  <<< ' + '; '.join(problems)))

print('\n全部通过' if ok else '\n存在问题')

# --------------------------------------------------------------------------- #
# 可选：合成新增图标在浅色 / 深色背景下的预览图
# --------------------------------------------------------------------------- #
try:
    from PIL import Image
except ImportError:
    print('\n（未安装 Pillow，跳过图标预览）')
    raise SystemExit(0 if ok else 1)

size, first = 32, 24
w, h, _, px = bmp((ROOT / f'res/Toolbar{size}.bmp').read_bytes())
tiles = []
for i in range(first, NEW_ICONS):
    tile = Image.new('RGBA', (size, size))
    tile.putdata([
        tuple(px[(h - 1 - y) * w * 4 + (i * size + x) * 4:][:4])
        for y in range(h) for x in range(size)
    ])
    tiles.append(tile)

scale, plate = 3, 20
board = Image.new('RGB', (size * len(tiles) * scale, size * 2 * scale + plate), (128, 128, 128))
for bg, row in (((255, 255, 255), 0), ((32, 32, 32), 1)):
    for idx, tile in enumerate(tiles):
        layer = Image.new('RGBA', (size, size), bg + (255,))
        layer.alpha_composite(tile)
        board.paste(layer.convert('RGB'),
                    (idx * size * scale, plate // 2 + row * (size * scale + plate // 2)))

out = ROOT / '.build/preview_toolbar_icons.png'
out.parent.mkdir(exist_ok=True)
board.save(out)
print(f'\n图标预览已保存: {out.name}  (上排浅色背景 / 下排深色背景；图标 24,25,26,27=上一个, 28=下一个)')
raise SystemExit(0 if ok else 1)
