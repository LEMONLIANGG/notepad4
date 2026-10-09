"""给 Notepad4 的文件菜单加上「上一个文件 / 下一个文件」，并在主加速键表里绑定 Alt+Left / Alt+Right。

同时处理 src/Notepad4.rc 和 locale/*/Notepad4.rc。
这些文件是 UTF-8 无 BOM + CRLF，脚本严格保持原编码与换行。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

MENU_ANCHOR = 'IDM_FILE_OPEN_CONTAINING_FOLDER'
ACCEL_ANCHOR_ID = 'IDR_MAINWND ACCELERATORS'

# 语言目录 -> (上一个, 下一个)
TEXTS = {
    '.': ('Previous File', 'Next File'),
    'de': ('Vorherige Datei', 'Nächste Datei'),
    'fr': ('Fichier précédent', 'Fichier suivant'),
    'it': ('File precedente', 'File successivo'),
    'ja': ('前のファイル', '次のファイル'),
    'ko': ('이전 파일', '다음 파일'),
    'pl': ('Poprzedni plik', 'Następny plik'),
    'pt-BR': ('Arquivo anterior', 'Próximo arquivo'),
    'ru': ('Предыдущий файл', 'Следующий файл'),
    'sl': ('Prejšnja datoteka', 'Naslednja datoteka'),
    'zh-Hans': ('上一个文件', '下一个文件'),
    'zh-Hant': ('上一個檔案', '下一個檔案'),
}


def rc_path(locale):
    return ROOT / 'src/Notepad4.rc' if locale == '.' else ROOT / f'locale/{locale}/Notepad4.rc'


def patch(locale):
    path = rc_path(locale)
    raw = path.read_bytes()
    if raw[:3] == b'\xef\xbb\xbf' or raw[:2] in (b'\xff\xfe', b'\xfe\xff'):
        raise SystemExit(f'{path}: 出现预期外的 BOM，请人工确认')
    text = raw.decode('utf-8')
    if '\r\n' not in text:
        raise SystemExit(f'{path}: 未检测到 CRLF')
    prev_text, next_text = TEXTS[locale]
    prev_text += '\\tAlt+Left'
    next_text += '\\tAlt+Right'

    lines = text.split('\r\n')
    out = []
    done_menu = False
    done_accel = False
    in_accel = False

    for line in lines:
        out.append(line)
        stripped = line.strip()

        if not done_menu and stripped.endswith(MENU_ANCHOR):
            indent = line[:len(line) - len(line.lstrip())]
            out.append(f'{indent}MENUITEM "{prev_text}",\t\tIDM_FILE_OPENPREV')
            out.append(f'{indent}MENUITEM "{next_text}",\t\tIDM_FILE_OPENNEXT')
            done_menu = True
            continue

        if stripped == ACCEL_ANCHOR_ID:
            in_accel = True
            continue
        if in_accel and not done_accel and stripped == 'BEGIN':
            out.append('    VK_LEFT,        IDM_FILE_OPENPREV,          VIRTKEY, ALT, NOINVERT')
            out.append('    VK_RIGHT,       IDM_FILE_OPENNEXT,          VIRTKEY, ALT, NOINVERT')
            done_accel = True
            in_accel = False
            continue
        if in_accel and stripped == 'END':
            in_accel = False

    if not (done_menu and done_accel):
        raise SystemExit(f'{path}: 插入失败 menu={done_menu} accel={done_accel}')

    path.write_bytes('\r\n'.join(out).encode('utf-8'))
    return done_menu, done_accel


def main():
    for locale in TEXTS:
        patch(locale)
        print(f'已更新 {rc_path(locale).relative_to(ROOT)}')


if __name__ == '__main__':
    sys.exit(main())
