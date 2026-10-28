# -*- coding: utf-8 -*-
"""抓屏层：定位游戏窗口 + 后台截屏（PrintWindow，游戏在后台也能抓）。

RE 版的「眼睛」。所有识别都从这里拿画面。
"""
import ctypes
import ctypes.wintypes as wt

import cv2
import numpy as np
import win32con
import win32gui
import win32ui

WM_KEYDOWN = 0x0100


def content_box(img):
    """在抓屏里自动定位游戏内容区（非黑 bounding box）。

    游戏窗口客户区可能远大于画面（窗口化/缩放），内容不一定居中。
    返回 (x, y, w, h)；全黑返回 None。
    """
    import numpy as np
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    ys, xs = np.where(gray > 16)
    if len(xs) < 100:
        return None
    # 裁掉离群噪点：取 0.2%~99.8% 分位
    x1, x2 = int(np.percentile(xs, 0.2)), int(np.percentile(xs, 99.8))
    y1, y2 = int(np.percentile(ys, 0.2)), int(np.percentile(ys, 99.8))
    return (x1, y1, x2 - x1 + 1, y2 - y1 + 1)


def grab_content(hwnd=None):
    """抓屏并裁出内容区。返回 (img, box)；box=None 时返回整屏。"""
    img = grab(hwnd)
    if img is None:
        return None, None
    box = content_box(img)
    if not box:
        return img, None
    x, y, w, h = box
    return img[y:y+h, x:x+w], box


def activate(hwnd):
    """把游戏窗口提前台（点击前必须，SendInput 只打最顶层窗口）。

    SetForegroundWindow 有系统限制（前台锁），用 AttachThreadInput 绕。
    返回 True=激活成功。
    """
    import ctypes
    import win32gui
    user32 = ctypes.windll.user32
    if win32gui.GetForegroundWindow() == hwnd:
        return True
    fg = user32.GetForegroundWindow()
    fg_t = user32.GetWindowThreadProcessId(fg, None)
    my_t = user32.GetWindowThreadProcessId(hwnd, None)
    cur_t = ctypes.windll.kernel32.GetCurrentThreadId()
    ok = True
    if fg_t and fg_t != my_t:
        user32.AttachThreadInput(fg_t, cur_t, True)
    user32.AttachThreadInput(my_t, cur_t, True)
    try:
        win32gui.ShowWindow(hwnd, 9)          # SW_RESTORE
        ok = bool(user32.SetForegroundWindow(hwnd))
    finally:
        if my_t:
            user32.AttachThreadInput(my_t, cur_t, False)
        if fg_t and fg_t != my_t:
            user32.AttachThreadInput(fg_t, cur_t, False)
    import time
    time.sleep(0.25)
    return ok


def at_top(hwnd, cx, cy):
    """校验屏幕坐标 (cx,cy) 处的最顶层窗口是不是 hwnd。"""
    import ctypes
    import ctypes.wintypes as wt
    pt = wt.POINT(int(cx), int(cy))
    top = ctypes.windll.user32.WindowFromPoint(pt)
    # 子窗口也会返回，向上找根
    root = ctypes.windll.user32.GetAncestor(top, 2)   # GA_ROOT
    return root == hwnd or top == hwnd


def find_game_window():
    """按进程 exe 名找游戏窗口（标题匹配会撞上助手/文件夹窗口）。

    匹配 QuestAway*.exe 且排除助手自己（QuestAwayHelper）。
    返回 (hwnd, 客户区宽, 高)；找不到返回 (0, 0, 0)。
    """
    result = []
    buf = ctypes.create_unicode_buffer(512)

    def cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd) or not win32gui.GetWindowText(hwnd):
            return
        pid = wt.DWORD()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        h = ctypes.windll.kernel32.OpenProcess(0x0410, False, pid.value)
        if not h:
            return
        try:
            n = ctypes.c_ulong(512)
            ctypes.windll.psapi.GetModuleFileNameExW(h, None, buf, n)
            exe = buf.value.replace("/", "\\").rsplit("\\", 1)[-1].lower()
            if exe.startswith("questaway") and "helper" not in exe:
                result.append(hwnd)
        finally:
            ctypes.windll.kernel32.CloseHandle(h)

    win32gui.EnumWindows(cb, None)
    # 游戏有多个窗口（主窗 + 通知小弹窗），选客户区面积最大的
    if not result:
        return 0, 0, 0
    hwnd = max(result, key=lambda h: (lambda r: (r[2]-r[0]) * (r[3]-r[1]))(
        win32gui.GetClientRect(h)))
    l, t, r, b = win32gui.GetClientRect(hwnd)
    return hwnd, r - l, b - t


def grab(hwnd=None):
    """后台抓游戏客户区。返回 BGR ndarray；窗口不在返回 None。

    PrintWindow 走窗口自身渲染管线，游戏被遮挡/最小化以外都能抓。
    最小化时抓不到 —— 挂机时窗口别最小化（可以挡住）。
    """
    if hwnd is None:
        hwnd, w, h = find_game_window()
        if not hwnd:
            return None
    else:
        l, t, r, b = win32gui.GetClientRect(hwnd)
        w, h = r - l, b - t
    if w <= 0 or h <= 0:
        return None

    hdc = win32gui.GetWindowDC(hwnd)
    mfc = win32ui.CreateDCFromHandle(hdc)
    save = mfc.CreateCompatibleDC()
    bmp = win32ui.CreateBitmap()
    bmp.CreateCompatibleBitmap(mfc, w, h)
    save.SelectObject(bmp)
    # PW_RENDERFULLCONTENT=2：Win8.1+，能抓 DirectComposition 内容（WebView2 必须）
    ok = ctypes.windll.user32.PrintWindow(hwnd, save.GetSafeHdc(), 2)
    buf = bmp.GetBitmapBits(True)
    img = np.frombuffer(buf, dtype=np.uint8).reshape((h, w, 4))
    win32gui.DeleteObject(bmp.GetHandle())
    save.DeleteDC()
    mfc.DeleteDC()
    win32gui.ReleaseDC(hwnd, hdc)
    if not ok:
        return None
    return cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)


def client_center(hwnd):
    """客户区中心在屏幕上的绝对坐标（SendInput 用）。"""
    pt = wt.POINT(0, 0)
    ctypes.windll.user32.ClientToScreen(hwnd, ctypes.byref(pt))
    l, t, r, b = win32gui.GetClientRect(hwnd)
    return pt.x + (r - l) // 2, pt.y + (b - t) // 2


def client_to_screen(hwnd, cx, cy):
    """客户区坐标 → 屏幕绝对坐标。"""
    pt = wt.POINT(int(cx), int(cy))
    ctypes.windll.user32.ClientToScreen(hwnd, ctypes.byref(pt))
    return pt.x, pt.y
