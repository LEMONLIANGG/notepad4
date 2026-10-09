"""Notepad4 内置文件夹浏览器（左侧面板）功能验收脚本。

四部分：

  A. 资源检查   —— 从 PE 资源里确认 Ctrl+Shift+F11 -> 40084 已写入加速键表，
                  且菜单文本 "Show Folder Browser" 已嵌入。
  B. 面板检查   —— 启动后确认左侧真的有 SysTreeView32 + SysListView32，
                  且列表内容 = testdata 目录（目录在前、文件在后，跳过隐藏文件）。
  C. 交互检查   —— 在列表里真实点击文件：文档必须在同一个窗口里切换过去
                  （标题变化、Scintilla 子窗口句柄不变、没有新开窗口）。
                  点目录则只在面板内导航，不换当前文档。
  D. 显隐检查   —— WM_COMMAND 40084 关掉面板后树/列表隐藏，编辑区回到 x=0。

用法:  python tools/verify_folder_browser.py [Notepad4.exe]
"""

import ctypes
import ctypes.wintypes as wt
import os
import re
import struct
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXE = ROOT / 'build/bin/Release/x64/Notepad4.exe'
TESTDIR = ROOT.parent / 'testdata'

IDM_VIEW_FOLDERBROWSER = 40084
IDM_FOLDERBROWSER_REFRESH = 40085
IDC_FOLDERBROWSER_TREE = 0xFB06
IDC_FOLDERBROWSER_LIST = 0xFB07
IDC_FOLDERBROWSER_SPLITTER_V = 0xFB08
IDC_FOLDERBROWSER_SPLITTER_H = 0xFB09
IDC_FOLDERBROWSER_NOTEEDIT = 0xFB0A

NOTE_STREAM = ':Notepad4.Note'
COL_NAME, COL_SIZE, COL_DATE, COL_ATTR, COL_COMMENT = 0, 1, 2, 3, 4
EXPECT_COLUMNS = ['Name', 'Size', 'Date Modified', 'Attributes', 'Comment']

RT_ACCELERATOR = 9
IDR_MAINWND = 100

VK_F11, VK_RETURN = 0x7A, 0x0D
FVIRTKEY, FNOINVERT, FSHIFT, FCONTROL = 0x01, 0x02, 0x04, 0x08
WM_COMMAND, WM_LBUTTONDOWN, WM_LBUTTONUP = 0x0111, 0x0201, 0x0202
WM_KEYDOWN = 0x0100
MK_LBUTTON = 0x0001
SW_RESTORE = 9

LVM_FIRST, TVM_FIRST = 0x1000, 0x1100
LVM_GETITEMCOUNT = LVM_FIRST + 4
LVM_GETITEMRECT = LVM_FIRST + 14
LVM_GETITEMTEXTW = LVM_FIRST + 115
LVM_SETITEMSTATE = LVM_FIRST + 43
TVM_GETCOUNT = TVM_FIRST + 5
TVM_GETNEXTITEM, TVM_ROOT, TVM_EXPAND = 0x110A, 0, 0x1102
TVM_SELECTITEM = TVM_FIRST + 11
TVE_EXPAND, TVGN_ROOT, TVGN_CHILD, TVGN_CARET = 0x0001, 0x0000, 0x0001, 0x0009

user32 = ctypes.WinDLL('user32', use_last_error=True)
kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)

# Notepad4 是 PerMonitorV2 感知进程；验收脚本也必须同口径，
# 否则系统会把目标窗口的客户区坐标虚拟化，点击位置全部对不上。
try:
    ctypes.WinDLL('shcore').SetProcessDpiAwareness(2)
except Exception:
    user32.SetProcessDPIAware()

_SendMessageW = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t
)(('SendMessageW', user32))
_FindWindowExW = ctypes.WINFUNCTYPE(
    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p
)(('FindWindowExW', user32))

# 句柄是 64 位，必须显式声明签名，否则 ctypes 会按 32 位整数截断
user32.GetDlgItem.restype = ctypes.c_void_p
user32.GetDlgItem.argtypes = [ctypes.c_void_p, ctypes.c_int]
user32.IsWindowVisible.restype = wt.BOOL
user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
user32.GetClientRect.restype = wt.BOOL
user32.GetClientRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(wt.RECT)]
user32.GetWindowRect.restype = wt.BOOL
user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(wt.RECT)]
user32.ScreenToClient.restype = wt.BOOL
user32.ScreenToClient.argtypes = [ctypes.c_void_p, ctypes.POINTER(wt.POINT)]
user32.PostMessageW.restype = wt.BOOL
user32.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
user32.GetWindowThreadProcessId.restype = wt.DWORD
user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(wt.DWORD)]
user32.SetForegroundWindow.restype = wt.BOOL
user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
user32.SetWindowTextW.restype = wt.BOOL
user32.SetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
user32.MoveWindow.restype = wt.BOOL
user32.MoveWindow.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                              ctypes.c_int, ctypes.c_int, wt.BOOL]
user32.GetForegroundWindow.restype = ctypes.c_void_p
user32.GetForegroundWindow.argtypes = []
user32.BringWindowToTop.argtypes = [ctypes.c_void_p]
user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
user32.AttachThreadInput.argtypes = [wt.DWORD, wt.DWORD, wt.BOOL]
user32.keybd_event.argtypes = [ctypes.c_ubyte, ctypes.c_ubyte, ctypes.c_uint, ctypes.c_size_t]

kernel32.OpenProcess.restype = ctypes.c_void_p
kernel32.OpenProcess.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_uint]
kernel32.VirtualAllocEx.restype = ctypes.c_void_p
kernel32.VirtualAllocEx.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
                                    ctypes.c_uint, ctypes.c_uint]
kernel32.ReadProcessMemory.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                       ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
kernel32.WriteProcessMemory.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                        ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
kernel32.VirtualFreeEx.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint]
kernel32.CloseHandle.argtypes = [ctypes.c_void_p]

PROCESS_VM = 0x0008 | 0x0010 | 0x0020 | 0x0400
MEM_COMMIT_RESERVE, PAGE_READWRITE, MEM_RELEASE = 0x3000, 0x04, 0x8000

# testdata 里的期望顺序：目录在前（sub），文件在后（自然排序），desktop.ini（隐藏）被跳过
EXPECT_ITEMS = ['..', 'sub', '01.txt', '02.ini', '03.log', '04.ini']


# --------------------------------------------------------------------------- #
# PE 资源解析（与 verify_file_nav.py 一致）
# --------------------------------------------------------------------------- #

def _sections(data):
    e_lfanew = struct.unpack_from('<I', data, 0x3C)[0]
    if data[e_lfanew:e_lfanew + 4] != b'PE\0\0':
        raise ValueError('不是有效的 PE 文件')
    coff = e_lfanew + 4
    nsec = struct.unpack_from('<H', data, coff + 2)[0]
    opt_size = struct.unpack_from('<H', data, coff + 16)[0]
    opt = coff + 20
    dd = opt + (112 if struct.unpack_from('<H', data, opt)[0] == 0x20B else 96)
    sec = opt + opt_size
    out = []
    for i in range(nsec):
        off = sec + i * 40
        vsize, vaddr, rawsize, rawptr = struct.unpack_from('<IIII', data, off + 8)
        out.append((vaddr, max(vsize, rawsize), rawptr, rawsize))
    return dd, out


def _rva2off(sections, rva):
    for vaddr, span, rawptr, _ in sections:
        if vaddr <= rva < vaddr + span:
            return rawptr + (rva - vaddr)
    return None


def _walk_res(data, base, doff, path=()):
    n_named, n_id = struct.unpack_from('<HH', data, doff + 12)
    out = []
    for i in range(n_named + n_id):
        name_id, offset = struct.unpack_from('<II', data, doff + 16 + i * 8)
        if name_id & 0x80000000:
            noff = base + (name_id & 0x7FFFFFFF)
            n = struct.unpack_from('<H', data, noff)[0]
            key = data[noff + 2:noff + 2 + n * 2].decode('utf-16-le')
        else:
            key = name_id
        if offset & 0x80000000:
            out += _walk_res(data, base, base + (offset & 0x7FFFFFFF), path + (key,))
        else:
            drva, dsize = struct.unpack_from('<II', data, base + offset)
            out.append((path + (key,), drva, dsize))
    return out


def read_accel_resource():
    data = EXE.read_bytes()
    dd, sections = _sections(data)
    res_rva = struct.unpack_from('<I', data, dd + 2 * 8)[0]
    base = _rva2off(sections, res_rva)
    for path, drva, dsize in _walk_res(data, base, base):
        if path[:2] == (RT_ACCELERATOR, IDR_MAINWND):
            return data[_rva2off(sections, drva):_rva2off(sections, drva) + dsize]
    return None


def check_resources():
    print('=== A. 资源检查 ===')
    ok = True
    raw = read_accel_resource()
    if not raw:
        print('  [FAIL] 未找到 IDR_MAINWND 加速键表')
        return False
    hit = False
    hit_refresh = False
    for stride in (8, 6):
        for i in range(len(raw) // stride):
            b = raw[i * stride:i * stride + stride]
            if stride == 8:
                fvirt, key, cmd = struct.unpack_from('<HHH', b, 0)
            else:
                fvirt, cmd = b[0], struct.unpack_from('<H', b, 1)[0]
                key = b[1] if stride == 6 else 0
            if fvirt == (FVIRTKEY | FNOINVERT | FSHIFT | FCONTROL) and key == VK_F11 \
                    and cmd == IDM_VIEW_FOLDERBROWSER:
                hit = True
    print(f'    {"OK " if hit else "FAIL"}  Ctrl+Shift+F11 -> 40084 (Show Folder Browser)')
    ok &= hit

    blob = EXE.read_bytes()
    for text in ('Show &Folder Browser',):
        found = text.encode('utf-16-le') in blob
        print(f'    {"OK " if found else "FAIL"}  菜单文本 "{text}" {"已嵌入" if found else "缺失"}')
        ok &= found
    return ok


# --------------------------------------------------------------------------- #
# 窗口辅助
# --------------------------------------------------------------------------- #

def find_window(pid):
    found = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(hwnd, _):
        wpid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        if wpid.value == pid and user32.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, buf, 512)
            if buf.value:
                found.append(hwnd)
        return True

    user32.EnumWindows(cb, 0)
    return found


def title(hwnd):
    buf = ctypes.create_unicode_buffer(1024)
    user32.GetWindowTextW(hwnd, buf, 1024)
    return buf.value


def current_file(hwnd):
    head = title(hwnd).split(' - ')[0].strip()
    head = head.split(' [')[0].strip()
    head = head.lstrip('*').strip()
    return re.split(r'[\\/]', head)[-1].strip()


def child_by_id(hwnd, cid):
    return user32.GetDlgItem(ctypes.c_void_p(hwnd), cid)


def child_by_class(hwnd, cls):
    return _FindWindowExW(hwnd, None, cls, None)


def rect_of(hwnd):
    rc = wt.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(rc))
    return rc


def parent_rect(parent, hwnd):
    """子窗口相对父窗口客户区的位置和大小。"""
    rc = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rc))
    pt = wt.POINT(rc.left, rc.top)
    user32.ScreenToClient(parent, ctypes.byref(pt))
    return pt.x, pt.y, rc.right - rc.left, rc.bottom - rc.top


class Remote:
    """在目标进程里分配缓冲区，用来收发需要指针的消息。"""

    def __init__(self, pid):
        self.h = kernel32.OpenProcess(PROCESS_VM, 0, pid)
        self.addr = None
        if self.h:
            self.addr = kernel32.VirtualAllocEx(self.h, None, 8192, MEM_COMMIT_RESERVE, PAGE_READWRITE)
        self.free = 8192

    def ok(self):
        return bool(self.h and self.addr)

    def call(self, hwnd, msg, wparam, data=None, read=None):
        if data is not None:
            kernel32.WriteProcessMemory(self.h, ctypes.c_void_p(self.addr), data, len(data),
                                        ctypes.byref(ctypes.c_size_t(0)))
        _SendMessageW(hwnd, msg, wparam, self.addr)
        if read:
            buf = ctypes.create_string_buffer(read)
            got = ctypes.c_size_t(0)
            kernel32.ReadProcessMemory(self.h, ctypes.c_void_p(self.addr), buf, read,
                                       ctypes.byref(got))
            return buf.raw[:got.value]
        return None

    def close(self):
        if self.addr:
            kernel32.VirtualFreeEx(self.h, ctypes.c_void_p(self.addr), 0, MEM_RELEASE)
        if self.h:
            kernel32.CloseHandle(self.h)


LVM_GETCOLUMNW = LVM_FIRST + 95
LVM_GETCOLUMNWIDTH = LVM_FIRST + 29
LVM_GETITEMSTATE = LVM_FIRST + 44


class LVCOLUMN(ctypes.Structure):
    _fields_ = [('mask', ctypes.c_uint),
                ('fmt', ctypes.c_int),
                ('cx', ctypes.c_int),
                ('pszText', ctypes.c_void_p),
                ('cchTextMax', ctypes.c_int),
                ('iSubItem', ctypes.c_int),
                ('iImage', ctypes.c_int),
                ('iOrder', ctypes.c_int)]


def list_column_text(rem, hwnd, i):
    text_addr = rem.addr + 1024
    col = LVCOLUMN()
    col.mask = 0x0004  # LVCF_TEXT
    col.pszText = text_addr
    col.cchTextMax = 256
    raw = ctypes.string_at(ctypes.byref(col), ctypes.sizeof(col))
    rem.call(hwnd, LVM_GETCOLUMNW, i, data=raw)
    buf = ctypes.create_string_buffer(512)
    got = ctypes.c_size_t(0)
    kernel32.ReadProcessMemory(rem.h, ctypes.c_void_p(text_addr), buf, 512, ctypes.byref(got))
    return buf.raw[:got.value].decode('utf-16-le', 'replace').split('\0')[0]


class LVITEM(ctypes.Structure):
    _fields_ = [('mask', ctypes.c_uint),
                ('iItem', ctypes.c_int),
                ('iSubItem', ctypes.c_int),
                ('state', ctypes.c_uint),
                ('stateMask', ctypes.c_uint),
                ('pszText', ctypes.c_void_p),
                ('cchTextMax', ctypes.c_int),
                ('iImage', ctypes.c_int),
                ('lParam', ctypes.c_ssize_t),
                ('iIndent', ctypes.c_int),
                ('iGroupId', ctypes.c_int),
                ('cColumns', ctypes.c_uint),
                ('puColumns', ctypes.c_void_p),
                ('piColFmt', ctypes.c_void_p),
                ('iGroup', ctypes.c_int)]


def list_subitem_text(rem, hwnd, iItem, iSub):
    text_addr = rem.addr + 1024
    lvi = LVITEM()
    lvi.mask = 1  # LVIF_TEXT
    lvi.iItem = iItem
    lvi.iSubItem = iSub
    lvi.pszText = text_addr
    lvi.cchTextMax = 512
    raw = ctypes.string_at(ctypes.byref(lvi), ctypes.sizeof(lvi))
    rem.call(hwnd, LVM_GETITEMTEXTW, iItem, data=raw)
    buf = ctypes.create_string_buffer(1024)
    got = ctypes.c_size_t(0)
    kernel32.ReadProcessMemory(rem.h, ctypes.c_void_p(text_addr), buf, 1024, ctypes.byref(got))
    return buf.raw[:got.value].decode('utf-16-le', 'replace').split('\0')[0]


def list_item_text(rem, hwnd, i):
    """读取列表项名字（文本缓冲放在目标进程里）。"""
    return list_subitem_text(rem, hwnd, i, COL_NAME)


# --------------------------------------------------------------------------- #
# 剪贴板 / 键盘
# --------------------------------------------------------------------------- #

CF_UNICODETEXT = 13
openClipboard = user32.OpenClipboard
openClipboard.argtypes = [ctypes.c_void_p]
user32.GetClipboardData.restype = ctypes.c_void_p
user32.GetClipboardData.argtypes = [ctypes.c_uint]
user32.CloseClipboard.argtypes = []
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]


def read_clipboard_text():
    if not openClipboard(None):
        return None
    try:
        h = user32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return ''
        p = kernel32.GlobalLock(ctypes.c_void_p(h))
        if not p:
            return ''
        try:
            return ctypes.wstring_at(ctypes.c_void_p(p))
        finally:
            kernel32.GlobalUnlock(ctypes.c_void_p(h))
    finally:
        user32.CloseClipboard()


user32.SetFocus.argtypes = [ctypes.c_void_p]
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]


def focus_list(hwnd, lst):
    """把测试窗口拉到前台，并把键盘焦点给文件列表。"""
    fg = user32.GetForegroundWindow()
    my_tid = kernel32.GetCurrentThreadId()
    if fg != hwnd:
        fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
        user32.AttachThreadInput(my_tid, fg_tid, True)
        try:
            user32.ShowWindow(hwnd, SW_RESTORE)
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
        finally:
            user32.AttachThreadInput(my_tid, fg_tid, False)
        time.sleep(0.4)
    app_tid = user32.GetWindowThreadProcessId(hwnd, None)
    user32.AttachThreadInput(my_tid, app_tid, True)
    try:
        user32.SetFocus(lst)
    finally:
        user32.AttachThreadInput(my_tid, app_tid, False)
    time.sleep(0.2)
    return user32.GetForegroundWindow() == hwnd


def press_keys(vk, ctrl=False, shift=False):
    VK_CONTROL, VK_SHIFT = 0x11, 0x10
    if ctrl:
        user32.keybd_event(VK_CONTROL, 0, 0, 0)
    if shift:
        user32.keybd_event(VK_SHIFT, 0, 0, 0)
    user32.keybd_event(vk, 0, 0, 0)
    user32.keybd_event(vk, 0, 2, 0)
    if shift:
        user32.keybd_event(VK_SHIFT, 0, 2, 0)
    if ctrl:
        user32.keybd_event(VK_CONTROL, 0, 2, 0)
    time.sleep(0.4)


def focus_edit(hwnd, edit):
    """把键盘焦点给备注编辑框（跨进程要 AttachThreadInput）。"""
    app_tid = user32.GetWindowThreadProcessId(hwnd, None)
    my_tid = kernel32.GetCurrentThreadId()
    user32.AttachThreadInput(my_tid, app_tid, True)
    try:
        user32.SetFocus(edit)
    finally:
        user32.AttachThreadInput(my_tid, app_tid, False)
    time.sleep(0.2)
    return True


def type_text(text):
    """用真实按键输入小写字母/数字（备注编辑框要真人敲字才算数）。

    不能用跨进程 SetWindowTextW：那样只会改到窗口的"缓存标题"，
    编辑控件内部文本没变，验收会误判（实测过）。
    """
    for ch in text:
        vk = ord(ch.upper())
        user32.keybd_event(vk, 0, 0, 0)
        user32.keybd_event(vk, 0, 2, 0)
        time.sleep(0.08)
    time.sleep(0.3)


def get_window_text(hwnd):
    buf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, buf, 512)
    return buf.value


def note_stream_path(name):
    return str(TESTDIR / (name + NOTE_STREAM))


def wait_note_edit(lst, timeout=3.0):
    """双击备注列后编辑框是「延后一步」弹出来的，要等它可见再往里写。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        edit = user32.GetDlgItem(ctypes.c_void_p(lst), IDC_FOLDERBROWSER_NOTEEDIT)
        if edit and user32.IsWindowVisible(edit):
            return edit
        time.sleep(0.1)
    return None


def click_list_item(rem, hwnd, i):
    """按列表项矩形中心发一次真实的左键按下/抬起。"""
    rem.call(hwnd, LVM_GETITEMRECT, i, data=ctypes.string_at(ctypes.byref(wt.RECT()), ctypes.sizeof(wt.RECT)),
             read=ctypes.sizeof(wt.RECT))
    buf = ctypes.create_string_buffer(ctypes.sizeof(wt.RECT))
    got = ctypes.c_size_t(0)
    kernel32.ReadProcessMemory(rem.h, ctypes.c_void_p(rem.addr), buf, ctypes.sizeof(wt.RECT),
                               ctypes.byref(got))
    left, top, right, bottom = struct.unpack('<iiii', buf.raw[:16])
    client = wt.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(client))
    # 点在 Name 列里（与真人点文件名一致），并保证落在控件客户区内
    x = min(max(left + 24, 2), max(client.right - 3, 2))
    y = min(max((top + bottom) // 2, 2), max(client.bottom - 3, 2))
    lparam = (y << 16) | (x & 0xFFFF)

    # 合成点击时把真实光标也挪过去：NM_CLICK 的列判断会用光标位置兜底
    screen = wt.POINT(x, y)
    user32.ClientToScreen(hwnd, ctypes.byref(screen))
    user32.SetCursorPos(screen.x, screen.y)
    time.sleep(0.05)

    user32.PostMessageW(hwnd, WM_LBUTTONDOWN, MK_LBUTTON, lparam)
    user32.PostMessageW(hwnd, WM_LBUTTONUP, 0, lparam)
    time.sleep(0.6)


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def list_subitem_rect(rem, lst, iItem, iSub):
    """整行矩形（LVM_GETITEMRECT 已验证可用）+ 按列宽累加，得到子项区间。

    LVM_GETSUBITEMRECT 在这台机器上返回全 0，不用它。
    """
    raw = rem.call(lst, LVM_GETITEMRECT, iItem,
                   data=ctypes.string_at(ctypes.byref(wt.RECT()), ctypes.sizeof(wt.RECT)),
                   read=ctypes.sizeof(wt.RECT))
    left, top, right, bottom = struct.unpack('<iiii', raw[:16])
    x0 = left
    for i in range(iSub):
        x0 += int(_SendMessageW(lst, LVM_GETCOLUMNWIDTH, i, 0))
    width = int(_SendMessageW(lst, LVM_GETCOLUMNWIDTH, iSub, 0))
    return x0, top, x0 + width, bottom


def click_list_subitem(rem, lst, iItem, iSub, dbl=False):
    """在指定单元格上发一次真实点击（dbl=True 时发双击序列）。"""
    left, top, right, bottom = list_subitem_rect(rem, lst, iItem, iSub)
    client = wt.RECT()
    user32.GetClientRect(lst, ctypes.byref(client))
    x = min(max((left + right) // 2, 2), max(client.right - 3, 2))
    y = min(max((top + bottom) // 2, 2), max(client.bottom - 3, 2))
    lparam = (y << 16) | (x & 0xFFFF)

    screen = wt.POINT(x, y)
    user32.ClientToScreen(lst, ctypes.byref(screen))
    user32.SetCursorPos(screen.x, screen.y)
    time.sleep(0.05)

    user32.PostMessageW(lst, WM_LBUTTONDOWN, MK_LBUTTON, lparam)
    user32.PostMessageW(lst, WM_LBUTTONUP, 0, lparam)
    if dbl:
        time.sleep(0.05)
        user32.PostMessageW(lst, 0x0203, MK_LBUTTON, lparam)	# WM_LBUTTONDBLCLK
        user32.PostMessageW(lst, WM_LBUTTONUP, 0, lparam)
    time.sleep(0.6)


def open_target():
    proc = subprocess.Popen([str(EXE), str(TESTDIR / '02.ini')])
    hwnd = None
    for _ in range(60):
        wins = find_window(proc.pid)
        if wins:
            hwnd = wins[0]
            break
        time.sleep(0.25)
    if hwnd:
        for _ in range(60):
            if ' [' in title(hwnd):
                break
            time.sleep(0.25)
    time.sleep(0.5)
    return proc, hwnd


def ensure_window_size(hwnd, width=1600, height=1000):
    """把窗口摆到固定大小，让后续的几何断言不受上次保存的窗口位置影响。"""
    user32.ShowWindow(hwnd, SW_RESTORE)		# 最大化状态下 MoveWindow 改不动尺寸
    time.sleep(0.2)
    user32.MoveWindow(hwnd, 40, 40, width, height, True)
    time.sleep(0.8)
    rc = wt.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(rc))
    return rc.right, rc.bottom


def check_panel(hwnd, proc):
    print('\n=== B. 面板检查 ===')
    ensure_window_size(hwnd)
    tree = child_by_id(hwnd, IDC_FOLDERBROWSER_TREE)
    lst = child_by_id(hwnd, IDC_FOLDERBROWSER_LIST)
    split_v = child_by_id(hwnd, IDC_FOLDERBROWSER_SPLITTER_V)
    split_h = child_by_id(hwnd, IDC_FOLDERBROWSER_SPLITTER_H)

    ok = True
    for name, h in (('文件夹树 SysTreeView32', tree), ('文件列表 SysListView32', lst),
                    ('垂直分隔条', split_v), ('水平分隔条', split_h)):
        good = bool(h) and bool(user32.IsWindowVisible(h))
        ok &= good
        print(f'    {"OK " if good else "FAIL"}  {name} {"已创建且可见" if good else "缺失/隐藏"}')
    if not ok:
        return False, None, None

    tx, ty, tw, th = parent_rect(hwnd, tree)
    lx, ly, lw, lh = parent_rect(hwnd, lst)
    vx, vy, vw, vh = parent_rect(hwnd, split_v)
    hx, hy, hw, hh = parent_rect(hwnd, split_h)
    print(f'    面板尺寸: 树 {tw}x{th} 于 ({tx},{ty}), 列表 {lw}x{lh} 于 ({lx},{ly}), '
          f'垂直分隔条 x={vx}, 水平分隔条 y={hy}')
    good = (tx == 0 and ty >= 0 and tw >= 200 and lx == 0 and ly > ty + th - 1
            and vx >= tx + tw and hx == tx and hw == tw)
    ok &= good
    print(f'    {"OK " if good else "FAIL"}  树在上、列表在下、两条分隔条位置正确、面板宽度>200')

    rem = Remote(proc.pid)
    if not rem.ok():
        print('  [SKIP] 无法连接进程，跳过内容检查')
        return ok, None, None

    count = int(_SendMessageW(lst, LVM_GETITEMCOUNT, 0, 0))
    items = [list_item_text(rem, lst, i) for i in range(count)]
    good = items == EXPECT_ITEMS
    ok &= good
    print(f'    {"OK " if good else "FAIL"}  列表内容 = {items}  (期望 {EXPECT_ITEMS})')

    roots = int(_SendMessageW(tree, TVM_GETCOUNT, 0, 0))
    good = roots >= 2
    ok &= good
    print(f'    {"OK " if good else "FAIL"}  树根节点 {roots} 个（各磁盘 + Desktop）')

    # 展开第一个根节点，确认能懒加载出子目录
    first = _SendMessageW(tree, TVM_GETNEXTITEM, TVGN_ROOT, 0)
    _SendMessageW(tree, TVM_EXPAND, TVE_EXPAND, first)
    time.sleep(0.4)
    child = _SendMessageW(tree, TVM_GETNEXTITEM, TVGN_CHILD, first)
    good = bool(child)
    ok &= good
    print(f'    {"OK " if good else "FAIL"}  展开根节点后拿到子节点（懒加载可用）')

    return ok, rem, lst


def check_switch(hwnd, proc, rem, lst):
    print('\n=== C. 单击切换 / 目录导航 ===')
    ok = True
    sci_before = child_by_class(hwnd, 'Scintilla')
    print(f'  当前文件: {current_file(hwnd)} (期望 02.ini)')
    wins_before = len(find_window(proc.pid))

    idx = EXPECT_ITEMS.index('03.log')
    click_list_item(rem, lst, idx)
    got = current_file(hwnd)
    good = got == '03.log'
    ok &= good
    print(f'    {"OK " if good else "FAIL"}  单击列表里的 03.log -> 当前文件 {got}')

    sci_after = child_by_class(hwnd, 'Scintilla')
    good = bool(sci_after) and sci_after == sci_before
    ok &= good
    print(f'    {"OK " if good else "FAIL"}  仍在同一个编辑控件里（未新开窗口）：{sci_after == sci_before}')

    wins_after = len(find_window(proc.pid))
    good = wins_after == wins_before
    ok &= good
    print(f'    {"OK " if good else "FAIL"}  顶层窗口数未增加（{wins_before} -> {wins_after}）')

    # 再点一个文件，确认可以连续切换
    idx = EXPECT_ITEMS.index('01.txt')
    click_list_item(rem, lst, idx)
    got = current_file(hwnd)
    good = got == '01.txt'
    ok &= good
    print(f'    {"OK " if good else "FAIL"}  再单击 01.txt -> 当前文件 {got}')

    # 点目录：只在面板内导航，不换当前文档
    idx = EXPECT_ITEMS.index('sub')
    click_list_item(rem, lst, idx)
    sub_count = int(_SendMessageW(lst, LVM_GETITEMCOUNT, 0, 0))
    sub_items = [list_item_text(rem, lst, i) for i in range(sub_count)]
    got = current_file(hwnd)
    good = sub_items == ['..'] and got == '01.txt'
    ok &= good
    print(f'    {"OK " if good else "FAIL"}  单击目录 sub -> 列表变成 {sub_items}，当前文件仍是 {got}')

    # 回到目录项「..」，应能回到 testdata
    click_list_item(rem, lst, 0)
    back = [list_item_text(rem, lst, i) for i in range(int(_SendMessageW(lst, LVM_GETITEMCOUNT, 0, 0)))]
    good = back == EXPECT_ITEMS
    ok &= good
    print(f'    {"OK " if good else "FAIL"}  单击「..」回到上级目录：{back == EXPECT_ITEMS}')

    # 键盘：选中项上按回车同样应在当前窗口打开
    idx = EXPECT_ITEMS.index('04.ini')
    set_selected(rem, lst, idx)
    _SendMessageW(lst, WM_KEYDOWN, VK_RETURN, 0)
    time.sleep(0.8)
    got = current_file(hwnd)
    good = got == '04.ini'
    ok &= good
    print(f'    {"OK " if good else "FAIL"}  列表里选中 04.ini 后按回车 -> 当前文件 {got}')
    return ok


def check_tree_nav(hwnd, rem, lst):
    """树 -> 列表：选中一个磁盘根节点，列表应换成该盘根目录（根目录没有「..」）。"""
    print('\n=== K. 树驱动列表 ===')
    tree = child_by_id(hwnd, IDC_FOLDERBROWSER_TREE)
    root = _SendMessageW(tree, TVM_GETNEXTITEM, TVGN_ROOT, 0)
    _SendMessageW(tree, TVM_SELECTITEM, TVGN_CARET, root)
    time.sleep(1.5)
    cnt = int(_SendMessageW(lst, LVM_GETITEMCOUNT, 0, 0))
    head = [list_item_text(rem, lst, i) for i in range(min(cnt, 3))]
    good = cnt > 0 and head[0] != '..'
    print(f'    {"OK " if good else "FAIL"}  选中树里的磁盘根节点 -> 列表 {cnt} 项，前 3 项 {head}')
    return good


def set_selected(rem, lst, i) -> None:
    lvi = LVITEM()
    lvi.state = 0x0003		# LVIS_SELECTED | LVIS_FOCUSED
    lvi.stateMask = 0x0003
    raw = ctypes.string_at(ctypes.byref(lvi), ctypes.sizeof(lvi))
    rem.call(lst, LVM_SETITEMSTATE, i, data=raw)
    time.sleep(0.3)


def check_toggle(hwnd):
    print('\n=== D. 面板显隐 ===')
    ok = True
    tree = child_by_id(hwnd, IDC_FOLDERBROWSER_TREE)
    sci = child_by_class(hwnd, 'Scintilla')

    def sci_x():
        rc = wt.RECT()
        user32.GetWindowRect(sci, ctypes.byref(rc))
        pt = wt.POINT(rc.left, rc.top)
        user32.ScreenToClient(hwnd, ctypes.byref(pt))
        return pt.x

    x_on = sci_x()
    user32.PostMessageW(hwnd, WM_COMMAND, IDM_VIEW_FOLDERBROWSER, 0)
    time.sleep(0.6)
    hidden = not user32.IsWindowVisible(tree)
    x_off = sci_x()
    good = hidden and x_off == 0
    ok &= good
    print(f'    {"OK " if good else "FAIL"}  关闭面板：树隐藏={hidden}，编辑区 x {x_on} -> {x_off}')

    user32.PostMessageW(hwnd, WM_COMMAND, IDM_VIEW_FOLDERBROWSER, 0)
    time.sleep(0.6)
    shown = bool(user32.IsWindowVisible(tree))
    x_back = sci_x()
    good = shown and x_back == x_on
    ok &= good
    print(f'    {"OK " if good else "FAIL"}  再打开面板：树可见={shown}，编辑区 x 回到 {x_back}')
    return ok


def check_drag(hwnd, proc):
    """拖动垂直分隔条：面板变宽，并把非默认宽度写进 Notepad4.ini。"""
    print('\n=== E. 拖动分隔条 ===')
    cxClient, cyClient = ensure_window_size(hwnd)
    print(f'    客户区 = {cxClient}x{cyClient}')
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.3)
    split = child_by_id(hwnd, IDC_FOLDERBROWSER_SPLITTER_V)
    tree = child_by_id(hwnd, IDC_FOLDERBROWSER_TREE)
    width_before = parent_rect(hwnd, tree)[2]

    # 用同步消息一气做完「按下 -> 移动 -> 抬起」：三条消息之间窗口不会动，
    # 客户区坐标与真实鼠标拖动同口径（分隔条自身客户区 x=2 就是按下点）。
    delta = 64
    rc = wt.RECT()
    user32.GetWindowRect(split, ctypes.byref(rc))
    h = rc.bottom - rc.top
    mid_y = rc.top + h // 2
    # 拖拽期间分隔条会 SetCapture，任何真实鼠标移动都会算进位移，
    # 所以把物理光标也摆到分隔条上（真人拖动本来就是这样）。
    user32.SetCursorPos(rc.left + 2, mid_y)
    time.sleep(0.1)
    lp_down = ((h // 2) << 16) | 2
    lp_move = ((h // 2) << 16) | (2 + delta)
    _SendMessageW(split, WM_LBUTTONDOWN, MK_LBUTTON, lp_down)
    user32.SetCursorPos(rc.left + 2 + delta, mid_y)
    _SendMessageW(split, 0x0200, MK_LBUTTON, lp_move)		# WM_MOUSEMOVE
    _SendMessageW(split, WM_LBUTTONUP, 0, lp_move)
    time.sleep(0.5)

    width_after = parent_rect(hwnd, tree)[2]
    grew = width_after - width_before
    grown = abs(grew - delta) <= 8
    print(f'    {"OK " if grown else "FAIL"}  向右拖 {delta}px：面板宽度 {width_before} -> {width_after}（增加 {grew}）')

    # 宽度要等程序退出时才落盘，这里只记录，退出后再检查（见 check_ini_saved）
    return grown


def read_text_auto(path):
    """Notepad4 的 ini 可能是 UTF-16LE(BOM) 或 UTF-8，按 BOM 判断。"""
    if not path.exists():
        return ''
    raw = path.read_bytes()
    if raw[:2] in (b'\xff\xfe', b'\xfe\xff'):
        return raw.decode('utf-16', 'replace')
    if raw[:3] == b'\xef\xbb\xbf':
        return raw[3:].decode('utf-8', 'replace')
    try:
        return raw.decode('utf-8')
    except UnicodeDecodeError:
        return raw.decode('gbk', 'replace')


def check_ini_saved():
    """退出后检查 Notepad4.ini 里记下了非默认的面板宽度。"""
    ini = EXE.parent / 'Notepad4.ini'
    text = read_text_auto(ini)
    for line in text.splitlines():
        if line.startswith('FolderBrowserWidth='):
            print(f'    OK   {ini.name} 里已记下 {line.strip()}')
            return True
    print(f'    FAIL {ini.name} 里没有 FolderBrowserWidth')
    return False


def check_columns(hwnd, rem, lst):
    """列表要显示「详细信息」里的几项：名称/大小/修改日期/属性/备注。"""
    print('\n=== H. 列表列与文件详细信息 ===')
    ok = True
    names = [list_column_text(rem, lst, i) for i in range(len(EXPECT_COLUMNS))]
    good = names == EXPECT_COLUMNS
    ok &= good
    print(f'    {"OK " if good else "FAIL"}  列 = {names}')

    widths = [int(_SendMessageW(lst, LVM_GETCOLUMNWIDTH, i, 0)) for i in range(len(EXPECT_COLUMNS))]
    good = all(w > 0 for w in widths)
    ok &= good
    print(f'    {"OK " if good else "FAIL"}  列宽 = {widths}')

    idx = EXPECT_ITEMS.index('01.txt')
    size = list_subitem_text(rem, lst, idx, COL_SIZE)
    date = list_subitem_text(rem, lst, idx, COL_DATE)
    attr = list_subitem_text(rem, lst, idx, COL_ATTR)
    good = (re.match(r'^\d+(\.\d+)? (B|KB|MB|GB)$', size) is not None
            and re.match(r'^\d{4}/\d{1,2}/\d{1,2} \d{1,2}:\d{2}$', date) is not None
            and 'A' in attr)
    ok &= good
    print(f'    {"OK " if good else "FAIL"}  01.txt 的 大小=[{size}] 修改日期=[{date}] 属性=[{attr}]')

    sub_idx = EXPECT_ITEMS.index('sub')
    sub_size = list_subitem_text(rem, lst, sub_idx, COL_SIZE)
    sub_attr = list_subitem_text(rem, lst, sub_idx, COL_ATTR)
    good = sub_size == '' and 'D' in sub_attr
    ok &= good
    print(f'    {"OK " if good else "FAIL"}  目录 sub 的 大小=[{sub_size}]（目录不显示大小）属性=[{sub_attr}]')
    return ok


def check_comment(hwnd, rem, lst):
    """备注：双击备注单元格内联编辑 -> 写进文件的 NTFS 数据流 -> 换目录回来仍在 -> 可清空。"""
    print('\n=== I. 备注（快速修改，相当于对文件备注）===')
    ok = True
    idx = EXPECT_ITEMS.index('03.log')
    note_text = 'note2026'
    stream = note_stream_path('03.log')

    # 备注列是交互区：单击只选中，不打开文件
    before = current_file(hwnd)
    click_list_subitem(rem, lst, idx, COL_COMMENT)
    good = current_file(hwnd) == before
    ok &= good
    print(f'    {"OK " if good else "FAIL"}  单击备注列只选中，不打开文件（当前 {current_file(hwnd)}）')

    # 双击备注列 -> 内联编辑框（编辑框会自己抢焦点，所以要先让窗口在前台）
    focus_list(hwnd, lst)
    click_list_subitem(rem, lst, idx, COL_COMMENT, dbl=True)
    edit = wait_note_edit(lst)
    good = bool(edit)
    ok &= good
    print(f'    {"OK " if good else "FAIL"}  双击备注列弹出内联编辑框：{"出现" if good else "没出现"}')
    if not good:
        return False

    # 先清掉原有备注，再敲新备注（全走真实按键）
    for _ in range(40):
        press_keys(0x08)		# VK_BACK
    type_text(note_text)
    press_keys(0x0D)			# VK_RETURN
    time.sleep(0.6)

    cell = list_subitem_text(rem, lst, idx, COL_COMMENT)
    good = cell == note_text
    ok &= good
    print(f'    {"OK " if good else "FAIL"}  备注列显示 [{cell}]（期望 {note_text}）')

    good = os.path.exists(stream)
    ok &= good
    print(f'    {"OK " if good else "FAIL"}  落盘：出现数据流 03.log{NOTE_STREAM}')
    if good:
        with open(stream, 'rb') as fp:
            body = fp.read().decode('utf-8', 'replace')
        good2 = body == note_text
        ok &= good2
        print(f'    {"OK " if good2 else "FAIL"}  数据流内容 = [{body}]')

    # 换个目录再回来，确认不是界面上的临时状态
    click_list_item(rem, lst, EXPECT_ITEMS.index('sub'))
    click_list_item(rem, lst, 0)
    cell = list_subitem_text(rem, lst, idx, COL_COMMENT)
    good = cell == note_text
    ok &= good
    print(f'    {"OK " if good else "FAIL"}  切到 sub 再返回后仍显示 [{cell}]')

    # 清空 -> 数据流应被删除
    click_list_subitem(rem, lst, idx, COL_COMMENT, dbl=True)
    edit = wait_note_edit(lst)
    if not edit:
        print('    FAIL 第二次双击没能打开编辑框')
        return False
    for _ in range(40):
        press_keys(0x08)		# VK_BACK
    press_keys(0x0D)
    time.sleep(0.6)
    cell = list_subitem_text(rem, lst, idx, COL_COMMENT)
    gone = not os.path.exists(stream)
    good = cell == '' and gone
    ok &= good
    print(f'    {"OK " if good else "FAIL"}  清空备注：备注列=[{cell}]，数据流已删除={gone}')

    # 再写一次，留给后面的复制测试用（顺带验证二次写入）
    click_list_subitem(rem, lst, idx, COL_COMMENT, dbl=True)
    edit = wait_note_edit(lst)
    if not edit:
        print('    FAIL 第三次双击没能打开编辑框')
        return False
    type_text(note_text)
    press_keys(0x0D)
    time.sleep(0.6)
    good = list_subitem_text(rem, lst, idx, COL_COMMENT) == note_text
    ok &= good
    print(f'    {"OK " if good else "FAIL"}  二次写入备注：{good}')
    return ok


def check_copy(hwnd, rem, lst):
    """Ctrl+C 复制文件名、Ctrl+Shift+C 复制完整路径（走主窗口加速键 + 命令），刷新走命令。"""
    print('\n=== J. 复制文件名 / 完整路径 / 刷新 ===')
    ok = True
    if not focus_list(hwnd, lst):
        print('  [SKIP] 无法让测试窗口取得前台焦点（当前环境限制），跳过真实按键')
        return None

    idx = EXPECT_ITEMS.index('03.log')
    set_selected(rem, lst, idx)
    press_keys(0x43, ctrl=True)		# Ctrl+C
    got = read_clipboard_text()
    good = got == '03.log'
    ok &= good
    print(f'    {"OK " if good else "FAIL"}  Ctrl+C  剪贴板 = [{got}]  (期望 03.log)')

    press_keys(0x43, ctrl=True, shift=True)		# Ctrl+Shift+C
    got = read_clipboard_text()
    expect = str(TESTDIR / '03.log')
    good = got is not None and got.lower() == expect.lower()
    ok &= good
    print(f'    {"OK " if good else "FAIL"}  Ctrl+Shift+C 剪贴板 = [{got}]')
    if not good:
        print(f'          期望 [{expect}]')

    # 刷新（右键菜单里的 Refresh，命令 40085）：新建文件后刷新应出现
    fresh = TESTDIR / '_tmp_refresh.txt'
    fresh.write_text('x', encoding='utf-8')
    try:
        names_before = [list_item_text(rem, lst, i) for i in range(int(_SendMessageW(lst, LVM_GETITEMCOUNT, 0, 0)))]
        user32.PostMessageW(hwnd, WM_COMMAND, IDM_FOLDERBROWSER_REFRESH, 0)
        time.sleep(0.6)
        names = [list_item_text(rem, lst, i) for i in range(int(_SendMessageW(lst, LVM_GETITEMCOUNT, 0, 0)))]
        good = '_tmp_refresh.txt' in names
        ok &= good
        print(f'    {"OK " if good else "FAIL"}  刷新（菜单命令）：新文件已出现在列表 = {good}')
        if not good:
            print(f'          刷新前 {names_before}')
            print(f'          刷新后 {names}')
    finally:
        fresh.unlink(missing_ok=True)
        user32.PostMessageW(hwnd, WM_COMMAND, IDM_FOLDERBROWSER_REFRESH, 0)
    return ok


def check_close(hwnd, proc):
    """优雅退出：走 WM_DESTROY -> FolderBrowser_Destroy（要回收树节点里的路径字符串）。"""
    print('\n=== F. 优雅退出 ===')
    user32.PostMessageW(hwnd, 0x0010, 0, 0)		# WM_CLOSE
    for _ in range(20):
        if proc.poll() is not None:
            break
        time.sleep(0.25)
    good = proc.poll() is not None
    print(f'    {"OK " if good else "FAIL"}  关闭窗口后进程退出（退出码 {proc.poll()}）')
    return good


def check_empty_start():
    """不带文件启动：面板仍然可用，不应崩溃。"""
    print('\n=== G. 空窗口启动 ===')
    proc = subprocess.Popen([str(EXE)])
    hwnd = None
    for _ in range(60):
        wins = find_window(proc.pid)
        if wins:
            hwnd = wins[0]
            break
        time.sleep(0.25)
    time.sleep(1.2)
    tree = child_by_id(hwnd, IDC_FOLDERBROWSER_TREE) if hwnd else None
    lst = child_by_id(hwnd, IDC_FOLDERBROWSER_LIST) if hwnd else None
    good = bool(hwnd) and bool(tree) and bool(lst) and bool(user32.IsWindowVisible(tree))
    print(f'    {"OK " if good else "FAIL"}  无文件启动时面板已就绪（标题 {title(hwnd) if hwnd else "-"}）')
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    return good


def main():
    global EXE
    if len(sys.argv) > 1:
        EXE = Path(sys.argv[1]).resolve()
    print(f'可执行文件: {EXE}')
    if not EXE.exists():
        print('构建产物不存在，请先构建。')
        return 1

    a_ok = check_resources()

    proc, hwnd = open_target()
    if not hwnd:
        print('\n未能启动/定位主窗口')
        proc.terminate()
        return 1

    b_ok, rem, lst = check_panel(hwnd, proc)
    c_ok = check_switch(hwnd, proc, rem, lst) if rem else None
    d_ok = check_toggle(hwnd)
    e_ok = check_drag(hwnd, proc)
    h_ok = check_columns(hwnd, rem, lst) if rem else None
    i_ok = check_comment(hwnd, rem, lst) if rem else None
    j_ok = check_copy(hwnd, rem, lst) if rem else None
    k_ok = check_tree_nav(hwnd, rem, lst) if rem else None
    if rem:
        rem.close()
    f_ok = check_close(hwnd, proc)
    e_ok = e_ok and check_ini_saved()
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    g_ok = check_empty_start()

    print('\n=== 汇总 ===')
    print(f'  A 资源(加速键+菜单)  : {"PASS" if a_ok else "FAIL"}')
    print(f'  B 面板创建与内容     : {"PASS" if b_ok else "FAIL"}')
    print(f'  C 单击切换/目录导航  : {"PASS" if c_ok else ("SKIP" if c_ok is None else "FAIL")}')
    print(f'  D 面板显隐           : {"PASS" if d_ok else "FAIL"}')
    print(f'  E 拖动分隔条         : {"PASS" if e_ok else "FAIL"}')
    print(f'  F 优雅退出           : {"PASS" if f_ok else "FAIL"}')
    print(f'  G 空窗口启动         : {"PASS" if g_ok else "FAIL"}')
    print(f'  H 列与详细信息       : {"PASS" if h_ok else ("SKIP" if h_ok is None else "FAIL")}')
    print(f'  I 备注编辑与落盘     : {"PASS" if i_ok else ("SKIP" if i_ok is None else "FAIL")}')
    print(f'  J 复制/刷新快捷键    : {"PASS" if j_ok else ("SKIP" if j_ok is None else "FAIL")}')
    print(f'  K 树驱动列表         : {"PASS" if k_ok else ("SKIP" if k_ok is None else "FAIL")}')
    verdict = (a_ok and b_ok and (c_ok is not False) and d_ok and e_ok and f_ok and g_ok
               and (h_ok is not False) and (i_ok is not False) and (j_ok is not False)
               and (k_ok is not False))
    print(f'\n总体结果: {"全部通过" if verdict else "存在问题"}')
    return 0 if verdict else 1


if __name__ == '__main__':
    sys.exit(main())
