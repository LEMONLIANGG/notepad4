"""把 res/Prev*.bmp 和 res/Next*.bmp 追加到 res/Toolbar*.bmp 图标的末尾。

- Toolbar{S}.bmp 是 32bpp BI_RGB 的横向图标条，含 27 个 SxS 图标，带真实 alpha 通道。
- Prev/Next{S}.bmp 是 4bpp 调色板位图，白色背景 + 绿色箭头，无 alpha。
  这里做 white-to-alpha 反解，还原抗锯齿透明度后居中贴进 SxS 方格。
- 追加后 Toolbar{S}.bmp 含 29 个图标：索引 27 = 上一个文件，索引 28 = 下一个文件。
"""

import struct
import sys
from pathlib import Path

SIZES = (16, 24, 32, 40, 48)
OLD_COUNT = 27
NEW_COUNT = 29
ARROW_RGB = (56, 138, 52)  # 箭头主色，用于反解 alpha


def read_pal4(path):
    """读取 4bpp 调色板位图，返回 (宽, 高, [[ (r,g,b) ], ...]) 自上而下的像素矩阵。"""
    raw = Path(path).read_bytes()
    off = struct.unpack('<I', raw[10:14])[0]
    w, h = struct.unpack('<ii', raw[18:26])
    bpp = struct.unpack('<H', raw[28:30])[0]
    if bpp != 4:
        raise ValueError(f'{path}: 期望 4bpp，实际 {bpp}')
    hdrsize = struct.unpack('<I', raw[14:18])[0]
    if hdrsize != 40:
        raise ValueError(f'{path}: 期望 40 字节 BITMAPINFOHEADER，实际 {hdrsize}')
    pal = []
    for i in range(1 << bpp):
        b, g, r, _ = raw[54 + i * 4:58 + i * 4]
        pal.append((r, g, b))
    stride = ((w * bpp + 31) // 32) * 4
    data = raw[off:off + stride * h]
    out = []
    for y in range(h):
        row = []
        for x in range(w):
            byte = data[(h - 1 - y) * stride + x // 2]
            idx = (byte >> 4) if x % 2 == 0 else (byte & 15)
            row.append(pal[idx])
        out.append(row)
    return w, h, out


def arrow_tile(path, size):
    """把 4bpp 箭头转成 (宽, size) 的 RGBA 像素块（list of list of (r,g,b,a)）。"""
    w, h, pixels = read_pal4(path)
    if h != size:
        raise ValueError(f'{path}: 高度 {h} != {size}')
    fg = ARROW_RGB
    tile = []
    for y in range(h):
        row = []
        for x in range(w):
            r, g, b = pixels[y][x]
            # 反解白底混合：以绿通道估计覆盖率
            denom = 255 - fg[1]
            cov = (255 - g) / denom if denom else 1.0
            a = max(0.0, min(1.0, cov))
            row.append((fg[0], fg[1], fg[2], int(round(a * 255))))
        tile.append(row)
    return w, tile


def extend_toolbar(root, size):
    path = root / f'res/Toolbar{size}.bmp'
    raw = path.read_bytes()
    off = struct.unpack('<I', raw[10:14])[0]
    header = raw[:off]
    w, h = struct.unpack('<ii', raw[18:26])
    bpp = struct.unpack('<H', raw[28:30])[0]
    if bpp != 32 or h != size or w != size * OLD_COUNT:
        raise ValueError(f'{path}: 预期 {size*OLD_COUNT}x{size} 32bpp，实际 {w}x{h} {bpp}bpp')
    stride = w * 4
    old = raw[off:off + stride * h]
    # 保留原图的 DPI 元数据，其余头部字段按新尺寸重建
    xppm, yppm = struct.unpack('<ii', raw[38:46])

    prev_w, prev_tile = arrow_tile(root / f'res/Prev{size}.bmp', size)
    next_w, next_tile = arrow_tile(root / f'res/Next{size}.bmp', size)

    new_w = size * NEW_COUNT
    new_stride = new_w * 4
    rows = []
    for i in range(h):
        # old 数据自上而下第 i 行在文件里是倒数第 i 行；统一转成自上而下再写回
        src = old[(h - 1 - i) * stride:(h - 1 - i) * stride + stride]
        line = bytearray(src) + bytearray(new_stride - stride)
        for name, aw, tile, base in (('Prev', prev_w, prev_tile, OLD_COUNT * size),
                                     ('Next', next_w, next_tile, (OLD_COUNT + 1) * size)):
            x0 = base + (size - aw) // 2
            for x in range(aw):
                r, g, b, a = tile[i][x]
                p = (x0 + x) * 4
                line[p + 0] = b
                line[p + 1] = g
                line[p + 2] = r
                line[p + 3] = a
        rows.append(bytes(line))

    pixel_data = b''.join(reversed(rows))  # BMP 自下而上

    bih = bytearray(struct.pack('<IiiHHIIiiII',
                                40, new_w, h, 1, 32, 0, len(pixel_data), xppm, yppm, 0, 0))
    bfh = struct.pack('<2sIHHI', b'BM', 14 + len(bih) + len(pixel_data), 0, 0, 14 + len(bih))
    path.write_bytes(bfh + bytes(bih) + pixel_data)
    return new_w, len(pixel_data)


def main():
    root = Path(__file__).resolve().parent.parent
    for size in SIZES:
        w, n = extend_toolbar(root, size)
        print(f'Toolbar{size}.bmp -> {w}px / {w // size} 个图标 / {n} 字节像素')


if __name__ == '__main__':
    sys.exit(main())
