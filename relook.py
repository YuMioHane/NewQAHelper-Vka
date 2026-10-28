# -*- coding: utf-8 -*-
"""识别层：模板匹配找 UI 元素。

RE 版的「眼睛」。抓屏 → 在内容区里找模板 → 返回客户区坐标（给 rehand 点击）。
模板存在 源码/tpl/，全部来自实机抓屏裁剪。
"""
import os

import cv2
import numpy as np

import recap


def imread_u(path):
    """中文路径安全读图（cv2.imread 不支持非 ASCII 路径）。"""
    return cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)


def imwrite_u(path, img):
    """中文路径安全写图。"""
    ext = os.path.splitext(path)[1] or ".png"
    ok, buf = cv2.imencode(ext, img)
    if ok:
        buf.tofile(path)
    return ok

TPL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tpl")
_TH = 0.80          # 匹配阈值（TM_CCOEFF_NORMED）
_cache = {}         # name -> (tpl, w, h)


def _tpl(name):
    if name not in _cache:
        p = os.path.join(TPL_DIR, name + ".png")
        t = imread_u(p)
        if t is None:
            raise FileNotFoundError("模板不存在: " + p)
        _cache[name] = (t, t.shape[1], t.shape[0])
    return _cache[name]


def find(img, name, th=_TH):
    """在抓屏（整幅客户区图）里找模板。

    返回客户区坐标 (cx, cy)（元素中心）；找不到返回 None。
    score 一并返回，(cx, cy, score)。
    """
    t, tw, thh = _tpl(name)
    res = cv2.matchTemplate(img, t, cv2.TM_CCOEFF_NORMED)
    _, mx, _, loc = cv2.minMaxLoc(res)
    if mx < th:
        return None
    return (loc[0] + tw // 2, loc[1] + thh // 2, round(mx, 3))


def find_all(img, name, th=_TH):
    """找全部命中（宝箱可能同时飘多个）。返回 [(cx, cy, score)] 按 x 排序。"""
    t, tw, thh = _tpl(name)
    res = cv2.matchTemplate(img, t, cv2.TM_CCOEFF_NORMED)
    xs, ys = np_where(res, th)
    out = []
    seen = []
    for x, y in zip(xs, ys):
        cx, cy = x + tw // 2, y + thh // 2
        if any(abs(cx - a) < tw and abs(cy - b) < thh for a, b in seen):
            continue                     # 同一个命中的相邻峰，去重
        seen.append((cx, cy))
        out.append((cx, cy, round(float(res[y, x]), 3)))
    out.sort(key=lambda p: p[0])
    return out


def np_where(res, th):
    ys, xs = np.where(res >= th)
    return list(xs), list(ys)


def wait_clickimg(hwnd, name, timeout=10.0):
    """轮询等元素出现（不点击）。返回客户区坐标或 None。"""
    import time
    t0 = time.time()
    while time.time() - t0 < timeout:
        img = recap.grab(hwnd)
        if img is not None:
            r = find(img, name)
            if r:
                return r
        time.sleep(0.5)
    return None


# ---------- 宝箱（三款） ----------

CHESTS = ("chest_wood", "chest_blue", "chest_gold")


def find_chests(img, th=0.75):
    """在内容区图里找所有宝箱。

    返回 [(cx, cy, kind, score)] 内容区坐标，按 x 排序。
    不同款模板命中同一位置时留分高的。
    """
    out = []
    for kind in CHESTS:
        for cx, cy, s in find_all(img, kind, th=th):
            out.append((cx, cy, kind, s))
    out.sort(key=lambda p: -p[3])
    kept = []
    for p in out:
        if all(abs(p[0] - q[0]) > 20 or abs(p[1] - q[1]) > 16 for q in kept):
            kept.append(p)
    kept.sort(key=lambda p: p[0])
    return kept


def to_screen(hwnd, box, cx, cy):
    """内容区坐标 → 屏幕坐标。box 是 grab_content 返回的内容区位置。"""
    ox = box[0] if box else 0
    oy = box[1] if box else 0
    return recap.client_to_screen(hwnd, ox + cx, oy + cy)
