# -*- coding: utf-8 -*-
"""人类输入层：硬件级 SendInput + 人类行为模拟。

RE 版的「手」。三条铁律：
1. 熵源必须是 secrets.SystemRandom（操作系统密码学熵），禁用可复现的
   伪随机序列 —— 每一次操作在物理上都是不可预测的。
2. 延迟不是均匀撒点：对数正态分布（中心 100~200ms，右偏长尾，
   偶尔「分神」），像真人反应时间。
3. 移动不是瞬移直线：随机控制点贝塞尔曲线 + 先加速后减速 + 微抖动，
   落点高斯散布在目标中心附近。

注意：SendInput 走系统鼠标队列，操作瞬间占用真实鼠标 —— 物理输入
没有后台。抓屏（recap.py）才是后台的。
"""
import ctypes
import ctypes.wintypes
import math
import secrets
import time

import win32api

_INP_MOUSE = 0
_INP_KEY = 1
_MOVE = 0x0001
_ABS = 0x8000
_LDOWN = 0x0002
_LUP = 0x0004
_RDOWN = 0x0008
_RUP = 0x0010
_WHEEL = 0x0800
_KEYUP = 0x0002

# 操作系统密码学熵源：种子来自硬件噪声，序列不可复现
rng = secrets.SystemRandom()


class _MI(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _KI(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
                ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _INP(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("mi", _MI), ("ki", _KI)]
    _fields_ = [("type", ctypes.c_ulong), ("u", _U)]


def _send(flags, dx=0, dy=0, data=0):
    i = _INP()
    i.type = _INP_MOUSE
    i.u.mi = _MI(dx, dy, data, flags, 0, None)
    ctypes.windll.user32.SendInput(1, ctypes.byref(i), ctypes.sizeof(_INP))


def _abs(x, y):
    sw, sh = win32api.GetSystemMetrics(0), win32api.GetSystemMetrics(1)
    return int(x * 65535 / (sw - 1)), int(y * 65535 / (sh - 1))


def _move_raw(x, y):
    ax, ay = _abs(x, y)
    _send(_MOVE | _ABS, ax, ay)


# ---------- 人类时序 ----------

def delay(lo=0.10, hi=0.20):
    """操作间延迟：对数正态（中心 lo~hi，右偏长尾），3% 概率分神。"""
    mid = (lo + hi) / 2
    d = rng.lognormvariate(math.log(mid) - 0.18, 0.32)
    d = max(lo * 0.75, min(hi * 1.7, d))
    if rng.random() < 0.03:                 # 偶尔人类走神
        d += rng.uniform(0.3, 0.9)
    time.sleep(d)


def _micro():
    """移动步间小停顿：人手不是匀速的。"""
    time.sleep(rng.uniform(0.006, 0.018))


# ---------- 人类轨迹 ----------

def _jitter_path(p1, p2):
    """p1→p2 的贝塞尔控制点：垂直于连线方向随机甩出去。"""
    (x1, y1), (x2, y2) = p1, p2
    dx, dy = x2 - x1, y2 - y1
    dist = math.hypot(dx, dy) or 1.0
    # 法向单位向量
    nx, ny = -dy / dist, dx / dist
    bow = dist * rng.uniform(0.06, 0.22) * rng.choice([-1, 1])
    c1 = (x1 + dx * 0.3 + nx * bow, y1 + dy * 0.3 + ny * bow)
    c2 = (x1 + dx * 0.7 + nx * bow * rng.uniform(0.3, 1.1),
          y1 + dy * 0.7 + ny * bow * rng.uniform(0.3, 1.1))
    return [p1, c1, c2, p2]


def _bezier(pts, t):
    """4 点贝塞尔取值。"""
    (x1, y1), (x2, y2), (x3, y3), (x4, y4) = pts
    mt = 1 - t
    x = mt**3*x1 + 3*mt**2*t*x2 + 3*mt*t**2*x3 + t**3*x4
    y = mt**3*y1 + 3*mt**2*t*y2 + 3*mt*t**2*y3 + t**3*y4
    return x, y


def _travel(x1, y1, x2, y2):
    """沿人类轨迹走过去：缓动速度 + 微抖 + 偶发过冲回拉。"""
    pts = _jitter_path((x1, y1), (x2, y2))
    steps = max(6, int(math.hypot(x2 - x1, y2 - y1) / rng.uniform(9, 18)))
    # sin 缓动：起步慢 → 中段快 → 收尾慢
    for i in range(1, steps + 1):
        t = i / steps
        te = 0.5 - 0.5 * math.cos(math.pi * t)
        x, y = _bezier(pts, te)
        x += rng.gauss(0, 0.7)              # 手抖
        y += rng.gauss(0, 0.7)
        _move_raw(x, y)
        _micro()
    # 8% 概率过冲再修回来（人手惯性）
    if rng.random() < 0.08:
        ox = x2 + (x2 - x1) / (math.hypot(x2-x1, y2-y1) or 1) * rng.uniform(4, 12)
        oy = y2 + (y2 - y1) / (math.hypot(x2-x1, y2-y1) or 1) * rng.uniform(4, 12)
        _move_raw(ox, oy)
        _micro()
        _move_raw(x2, y2)
        _micro()


def _spot(x, y, spread=3.5):
    """落点：目标中心附近高斯散布，不点正中心。"""
    return x + rng.gauss(0, spread), y + rng.gauss(0, spread * 0.8)


# ---------- 对外动作 ----------

def click(x, y, spread=3.5):
    """人类式单击：走轨迹 → 高斯落点 → 随机按下时长。"""
    cx, cy = _get_cursor()
    tx, ty = _spot(x, y, spread)
    _travel(cx, cy, tx, ty)
    delay(0.04, 0.09)                        # 瞄准确认
    _send(_LDOWN)
    time.sleep(rng.uniform(0.045, 0.11))     # 按住时长因人而异
    _send(_LUP)
    delay()


def rclick(x, y, spread=3.5):
    """人类式右键。"""
    cx, cy = _get_cursor()
    tx, ty = _spot(x, y, spread)
    _travel(cx, cy, tx, ty)
    delay(0.04, 0.09)
    _send(_RDOWN)
    time.sleep(rng.uniform(0.05, 0.10))
    _send(_RUP)
    delay()


def wheel(x, y, delta):
    """在目标处滚轮（先移过去，带人类轨迹）。"""
    cx, cy = _get_cursor()
    _travel(cx, cy, x, y)
    delay(0.05, 0.10)
    _send(_WHEEL, data=(delta * rng.choice([1, 1, 1, 2])) & 0xFFFFFFFF)
    delay()


def drag(x1, y1, x2, y2):
    """人类式拖拽：轨迹过去 → 按住 → 拖（缓动+抖）→ 松。"""
    cx, cy = _get_cursor()
    sx, sy = _spot(x1, y1)
    ex, ey = _spot(x2, y2)
    _travel(cx, cy, sx, sy)
    delay(0.05, 0.10)
    _send(_LDOWN)
    time.sleep(rng.uniform(0.09, 0.16))      # 抓稳
    _travel(sx, sy, ex, ey)
    time.sleep(rng.uniform(0.06, 0.12))      # 松手前停一下
    _send(_LUP)
    delay()


def key(vk, hold=None):
    """按键：按下+抬起。hold 秒 = 按住时长（None 则人类化随机）。"""
    _send_key(vk, 0)
    time.sleep(hold if hold is not None else rng.uniform(0.05, 0.12))
    _send_key(vk, _KEYUP)
    delay()


def _send_key(vk, flags):
    i = _INP()
    i.type = _INP_KEY
    i.u.ki = _KI(vk, 0, flags, 0, None)
    ctypes.windll.user32.SendInput(1, ctypes.byref(i), ctypes.sizeof(_INP))


ESC = 0x1B


def _get_cursor():
    pt = ctypes.wintypes.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y
