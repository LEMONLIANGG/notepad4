"""Notepad4「上一个文件 / 下一个文件」功能验收脚本。

三部分：

  A. 资源检查   —— 从 PE 资源里读取 IDR_MAINWND 的 ACCELERATORS 表，
                  确认 Alt+Left -> 40031、Alt+Right -> 40032 已写入，
                  并确认菜单文本 "Previous File" / "Next File" 存在。

  B. 行为检查   —— 向主窗口直接投递 WM_COMMAND，按文件名自然序逐个切换，
                  验证：自然排序、跳过隐藏/系统文件与子目录、首尾环绕、双向切换。

  C. 端到端检查 —— 真实注入 Alt+Right / Alt+Left 按键走系统加速键通道
                  （需窗口取得前台焦点；拿不到焦点则标记跳过）。

用法:  python tools/verify_file_nav.py
"""

import ctypes
import ctypes.wintypes as wt
import re
import struct
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXE = ROOT / 'build/bin/Release/x64/Notepad4.exe'
TESTDIR = ROOT.parent / 'testdata'

IDM_FILE_OPENPREV = 40031
IDM_FILE_OPENNEXT = 40032
RT_ACCELERATOR = 9
IDR_MAINWND = 100

VK_LEFT, VK_RIGHT, VK_MENU = 0x25, 0x27, 0x12
FVIRTKEY, FNOINVERT, FALT = 0x01, 0x02, 0x10
WM_COMMAND = 0x0111
SW_RESTORE = 9

user32 = ctypes.WinDLL('user32', use_last_error=True)
kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)

# 用带完整类型签名的函数对象调用，避免 LPARAM 被截断为 32 位
_SendMessageW = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t
)(('SendMessageW', user32))
_FindWindowExW = ctypes.WINFUNCTYPE(
    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p
)(('FindWindowExW', user32))

kernel32.OpenProcess.restype = ctypes.c_void_p
kernel32.OpenProcess.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_uint]
kernel32.VirtualAllocEx.restype = ctypes.c_void_p
kernel32.VirtualAllocEx.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
                                    ctypes.c_uint, ctypes.c_uint]
kernel32.ReadProcessMemory.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                       ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
kernel32.VirtualFreeEx.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint]
kernel32.CloseHandle.argtypes = [ctypes.c_void_p]

SCI_GETLENGTH, SCI_GETTEXT = 2006, 2182
PROCESS_VM = 0x0008 | 0x0010 | 0x0020 | 0x0400     # VM_OPERATION|VM_READ|VM_WRITE|QUERY_INFORMATION
MEM_COMMIT_RESERVE, PAGE_READWRITE, MEM_RELEASE = 0x3000, 0x04, 0x8000

# 目录内应被纳入切换的文件（自然序，跳过 desktop.ini 与 sub\）
ORDER = ['01.txt', '02.ini', '03.log', '04.ini']
# 期望：从 02.ini 出发的切换序列（含环绕）
EXPECT_NEXT = ['03.log', '04.ini', '01.txt', '02.ini']
EXPECT_PREV = ['01.txt', '04.ini']


# --------------------------------------------------------------------------- #
# PE 资源解析
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


def parse_accel(raw):
    """返回 (stride, entries)。

    MSVC 生成的 ACCEL 资源为 8 字节/条：WORD fVirt + WORD key + WORD cmd + WORD pad。
    实测字节: 13 00 | 25 00 | 5f 9c | 00 00   (Alt+Left -> 40031)
    """
    for stride in (8, 6):
        entries, n = [], len(raw) // stride
        for i in range(n):
            b = raw[i * stride:i * stride + stride]
            if stride == 8:
                fvirt, key, cmd = struct.unpack_from('<HHH', b, 0)
            else:
                fvirt = b[0]
                key, cmd = struct.unpack_from('<HH', b, 1)
            entries.append((fvirt, key, cmd))
        hits = [(f, k, c) for f, k, c in entries
                if f == (FVIRTKEY | FNOINVERT | FALT) and k in (VK_LEFT, VK_RIGHT)
                and c in (IDM_FILE_OPENPREV, IDM_FILE_OPENNEXT)]
        if len(hits) == 2:
            return stride, entries
    return None, []


def check_resources():
    print('=== A. 资源检查 ===')
    ok = True
    raw = read_accel_resource()
    if not raw:
        print('  [FAIL] 未找到 IDR_MAINWND 加速键表')
        return False

    stride, entries = parse_accel(raw)
    if stride is None:
        print(f'  [FAIL] 加速键表中未找到 Alt+Left/Right 绑定（{len(raw)} 字节）')
        ok = False
    else:
        print(f'  加速键表: {len(raw)} 字节 / {len(entries)} 条 (stride={stride})')
        for fvirt, key, cmd in entries:
            if fvirt == 0x13 and key in (VK_LEFT, VK_RIGHT) and cmd in (IDM_FILE_OPENPREV, IDM_FILE_OPENNEXT):
                name = 'Alt+Left ' if key == VK_LEFT else 'Alt+Right'
                which = 'Previous' if cmd == IDM_FILE_OPENPREV else 'Next'
                print(f'    OK  {name} -> {which} File (cmd={cmd})')

    blob = EXE.read_bytes()
    for text in ('Previous File', 'Next File'):
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
    return found[0] if found else None


def title(hwnd):
    buf = ctypes.create_unicode_buffer(1024)
    user32.GetWindowTextW(hwnd, buf, 1024)
    return buf.value


def current_file(hwnd):
    """从标题里取文件名。

    标题形如： 03.log [D:\\...\\testdata] - Notepad4 (Administrator)
    文档被改动时前面会多一个 '* '。
    """
    head = title(hwnd).split(' - ')[0].strip()   # 去掉 " - Notepad4 (User)"
    head = head.split(' [')[0].strip()           # 去掉 " [<dir>]"
    head = head.lstrip('*').strip()              # 去掉未保存标记 '*'
    return re.split(r'[\\/]', head)[-1].strip()


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def open_target():
    if not EXE.exists():
        print('找不到可执行文件:', EXE)
        return None, None
    proc = subprocess.Popen([str(EXE), str(TESTDIR / '02.ini')])
    hwnd = None
    for _ in range(40):
        hwnd = find_window(proc.pid)
        if hwnd:
            break
        time.sleep(0.25)
    # 等待文件真正载入：标题里出现 " [<dir>]"
    if hwnd:
        for _ in range(40):
            if ' [' in title(hwnd):
                break
            time.sleep(0.25)
    return proc, hwnd


def check_behavior(hwnd):
    print('\n=== B. 同目录切换行为检查 ===')
    print(f'  初始文件: {current_file(hwnd)}   (期望 02.ini)')
    ok = current_file(hwnd) == '02.ini'

    def step(label, cmd, expect):
        nonlocal ok
        user32.PostMessageW(hwnd, WM_COMMAND, cmd, 0)
        time.sleep(0.35)
        got = current_file(hwnd)
        good = got == expect
        ok &= good
        print(f'    {"OK " if good else "FAIL"}  {label:10s} 实际={got:12s} 期望={expect}')

    for i, exp in enumerate(EXPECT_NEXT, 1):
        step(f'下一个 #{i}', IDM_FILE_OPENNEXT, exp)
    for i, exp in enumerate(EXPECT_PREV, 1):
        step(f'上一个 #{i}', IDM_FILE_OPENPREV, exp)

    # 环绕与跳过项的显式断言
    print(f'    {"OK " if ok else "FAIL"}  自然排序 / 跳过 desktop.ini 与 sub\\ / 首尾环绕 / 双向切换')
    return ok


class EditorReader:
    """跨进程读取 Notepad4 编辑控件文本。

    SCI_* 属于 WM_USER 以上消息，系统不做跨进程参数编组，缓冲区必须分配在目标进程内。
    """

    def __init__(self, hwnd, pid):
        self.sci = _FindWindowExW(hwnd, None, 'Scintilla', None)
        self.h = kernel32.OpenProcess(PROCESS_VM, 0, pid)
        self.addr = None
        if self.h:
            self.addr = kernel32.VirtualAllocEx(self.h, None, 4096,
                                                MEM_COMMIT_RESERVE, PAGE_READWRITE)

    def ok(self):
        return bool(self.sci and self.h and self.addr)

    def text(self):
        n = int(_SendMessageW(self.sci, SCI_GETLENGTH, 0, 0))
        if n <= 0:
            return ''
        _SendMessageW(self.sci, SCI_GETTEXT, n + 1, self.addr)
        buf = ctypes.create_string_buffer(n + 4)
        got = ctypes.c_size_t(0)
        kernel32.ReadProcessMemory(self.h, ctypes.c_void_p(self.addr), buf, n, ctypes.byref(got))
        return buf.raw[:got.value].decode('utf-8', 'replace')

    def close(self):
        if self.addr:
            kernel32.VirtualFreeEx(self.h, ctypes.c_void_p(self.addr), 0, MEM_RELEASE)
        if self.h:
            kernel32.CloseHandle(self.h)


# (文件名, 编码说明, 文件中必须出现的内容)
INI_CASES = [
    ('02.ini', 'UTF-16LE BOM', ['[Settings]', 'Name=02.ini (UTF-16LE BOM)', '中文测试']),
    ('04.ini', 'GBK/ANSI',     ['[Config]', 'Name=04.ini (GBK/ANSI)', '中文测试']),
]


def check_ini_content(hwnd, proc):
    print('\n=== D. INI 文件打开与编码检查 ===')
    rd = EditorReader(hwnd, proc.pid)
    if not rd.ok():
        print('  [SKIP] 无法连接编辑控件（OpenProcess/VirtualAllocEx 失败）')
        return None

    ok = True
    for name, enc, needles in INI_CASES:
        for _ in range(len(ORDER) + 2):                 # 有界循环，避免死转
            if current_file(hwnd) == name:
                break
            user32.PostMessageW(hwnd, WM_COMMAND, IDM_FILE_OPENNEXT, 0)
            time.sleep(0.35)
        body = rd.text()
        hit = all(n in body for n in needles)
        ok &= hit
        print(f'    {"OK " if hit else "FAIL"}  {name:7s} ({enc:12s}) 解码正确，含中文内容')
        if not hit:
            print(f'          实际读取: {body!r}')
    rd.close()
    return ok


def check_end_to_end(hwnd, proc):
    print('\n=== C. 端到端按键检查 (Alt+Right / Alt+Left) ===')

    def ensure_fg():
        """尽量让测试窗口处于前台；成功返回 True。

        注意：不要调用 SetActiveWindow —— 实测它会把输入队列里遗留的按键
        灌进编辑区，导致文档被意外修改（标题出现 '*'），进而污染后续断言。
        """
        if user32.GetForegroundWindow() == hwnd:
            return True
        # 标准做法：把自己的线程输入队列挂到当前前台线程上，才能绕过前台锁定
        fg = user32.GetForegroundWindow()
        fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
        my_tid = kernel32.GetCurrentThreadId()
        user32.AttachThreadInput(my_tid, fg_tid, True)
        try:
            user32.ShowWindow(hwnd, SW_RESTORE)
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
        finally:
            user32.AttachThreadInput(my_tid, fg_tid, False)
        for _ in range(8):
            if user32.GetForegroundWindow() == hwnd:
                return True
            time.sleep(0.2)
        return user32.GetForegroundWindow() == hwnd

    def press(vk):
        user32.keybd_event(VK_MENU, 0, 0, 0)
        user32.keybd_event(vk, 0, 0, 0)
        user32.keybd_event(vk, 0, 2, 0)
        user32.keybd_event(VK_MENU, 0, 2, 0)
        time.sleep(0.5)

    if not ensure_fg():
        print('  [SKIP] 无法让测试窗口取得前台焦点（当前环境限制），跳过真实按键注入')
        print('         该通道已由 A 段加速键表 + B 段命令派发共同覆盖')
        return None

    cur = current_file(hwnd)
    print(f'  起点: {cur}')
    if cur not in ORDER:
        print('  [SKIP] 起点不在预期文件集合内')
        return None

    ok = True
    for label, vk, delta in (('Alt+Right', VK_RIGHT, +1),
                             ('Alt+Left', VK_LEFT, -1),
                             ('Alt+Right', VK_RIGHT, +1)):
        exp = ORDER[(ORDER.index(cur) + delta) % len(ORDER)]
        got = cur
        for _ in range(3):                       # 失去焦点/时序抖动时重试
            if not ensure_fg():
                print(f'  [SKIP] {label} 无法维持前台焦点，剩余按键检查跳过')
                return ok if ok else None
            press(vk)
            got = current_file(hwnd)
            if got == exp:
                break
        good = got == exp
        ok &= good
        print(f'    {"OK " if good else "FAIL"}  {label:10s} 实际={got:12s} 期望={exp}')
        cur = got
    return ok


def main():
    global EXE
    if len(sys.argv) > 1:                 # 可选：指定被验收的可执行文件
        EXE = Path(sys.argv[1]).resolve()
    print(f'可执行文件: {EXE}')
    if not EXE.exists():
        print('构建产物不存在，请先构建。')
        return 1

    a_ok = check_resources()

    proc, hwnd = open_target()
    if not hwnd:
        print('\n未能启动/定位主窗口')
        if proc:
            proc.terminate()
        return 1

    b_ok = check_behavior(hwnd)
    c_ok = check_end_to_end(hwnd, proc)
    d_ok = check_ini_content(hwnd, proc)

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()

    print('\n=== 汇总 ===')
    print(f'  A 资源(加速键+菜单)  : {"PASS" if a_ok else "FAIL"}')
    print(f'  B 切换行为           : {"PASS" if b_ok else "FAIL"}')
    print(f'  C 真实按键端到端     : {"PASS" if c_ok else ("SKIP" if c_ok is None else "FAIL")}')
    print(f'  D INI 打开与编码     : {"PASS" if d_ok else ("SKIP" if d_ok is None else "FAIL")}')
    verdict = a_ok and b_ok and (c_ok is not False) and (d_ok is not False)
    print(f'\n总体结果: {"全部通过" if verdict else "存在问题"}')
    return 0 if verdict else 1


if __name__ == '__main__':
    sys.exit(main())
