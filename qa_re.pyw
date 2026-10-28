# -*- coding: utf-8 -*-
"""QA助手：RE —— 不依赖调试端口的 QuestAway 挂机助手。

技术路线：PrintWindow 后台抓屏 + OpenCV 模板匹配（每步先识图再点击，不缓存坐标）
         + SendInput 真实点击（贝塞尔轨迹/高斯落点/密码学真随机延迟）。
点击时必须前台（挂机期间会占用鼠标）；抓屏与存档读取可后台。
"""
import collections
import io
import glob
import gzip
import json
import math
import queue
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

if sys.stdout:  # --noconsole 打包后 stdout 为 None，跳过重定向
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = sys.stdout

import os

# APPDIR：源码跑时=脚本目录；打包后=exe 所在目录（日志/令牌存这里，_MEIPASS 是只读临时目录）
APPDIR = os.path.dirname(sys.executable) if getattr(sys, "_MEIPASS", None) else os.path.dirname(os.path.abspath(__file__))
CAPTURE_DIR = os.path.join(APPDIR, "_capture")
TOKEN_FILE = os.path.join(APPDIR, "_token.txt")
GAME_EXE_DEFAULT = r"F:\Steam\steamapps\common\QuestAway\QuestAway.exe"

BASE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
TPLDIR = os.path.join(BASE, "tpl")
if os.path.dirname(os.path.abspath(__file__)) not in sys.path:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cv2
import numpy as np
import tkinter as tk
from tkinter import ttk, messagebox

import recap
import rehand
import relook

# 工作台按钮相对偏移（同窗口内部结构恒定）
CRAFT_DX = 118        # 自动填入 → 转化
CRAFT_BACK = (32, 37)  # 自动填入 → 全部取回（防材料格卡住）

_cache = {}

# 市场监控只在「市场版」exe 启用；普通版 exe 不带市场功能（源码调试时保留）
MARKET_BUILD = (not getattr(sys, "frozen", False)) or \
               ("市场版" in os.path.basename(getattr(sys, "executable", "")))

# ---- 收益分析数据（wiki 提取，随 exe 打包）----
def _load_json(name):
    for d in (BASE, os.path.dirname(os.path.abspath(__file__))):
        p = os.path.join(d, name)
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                return json.load(f)
    return None

XP_TABLE = _load_json("_xp_table.json") or []   # Lv1→2 需 XP_TABLE[0]，共 99 项到 Lv100
NAMES = _load_json("_names.json") or {"enemies": {}, "maps": {}}
STAGES = _load_json("_stages.json") or {}            # 地图 → 每关怪 HP/每击/金币/经验
CLASS_STATS = _load_json("_class_stats.json") or {}  # 职业 → 基础属性与成长
TREE = _load_json("_tree.json") or {}                # 天赋树效果与基石


# ---- 理论收益模型（qa.py 移植：普攻口径 DPS，对 0 护甲目标，未含技能循环/走位）----
CLASS_PAGE = {"knight": "warrior", "warrior": "warrior", "mage": "mage", "priest": "healer",
              "healer": "healer", "archer": "archer", "assassin": "assassin", "thief": "assassin",
              "engineer": "engineer", "barbarian": "barbarian", "monk": "monk",
              "witch": "witch", "wizard": "witch", "paladin": "paladin"}
PCT_STATS = {
    "haste", "castspeed", "cooldown", "cdr", "critchance", "critdmg", "fireres", "iceres",
    "lightres", "chaosresist", "allresist", "lifesteal", "fury", "shock", "curse", "weaken",
    "blind", "expose", "penetration", "elempend", "elempenetration", "movespeed", "goldfind",
    "healingdone", "spelllifesteal", "cursedmg", "curseeffect", "auraeffect", "dotspeed",
    "healpower", "healreceived", "allresistcap", "shield",
}


def _talent_bonus(nodes):
    """天赋树已点节点 → flat/pct/inc/more/penalty 加成表。"""
    effects = TREE.get("effects") or {}
    keystones = TREE.get("keystones") or {}
    flat = collections.defaultdict(float)
    pct = collections.defaultdict(float)
    inc = collections.defaultdict(float)
    more = collections.defaultdict(float)
    pen = collections.defaultdict(float)
    for nid in nodes:
        ks = keystones.get(str(nid))
        if ks:
            for k, v in (ks.get("more") or {}).items():
                more[str(k).lower()] += v
            for k, v in (ks.get("penalty") or {}).items():
                pen[str(k).lower()] += v
            continue
        for e in effects.get(str(nid), []):
            if len(e) < 3:
                continue
            st, kind, val = str(e[0]).lower(), e[1], e[2]
            if kind == "flat":
                flat[st] += val
            elif kind == "pct":
                pct[st] += val
            else:
                inc[st] += val
    return flat, pct, inc, more, pen


def _hero_dps(class_id, level, hero):
    """英雄估测 DPS：攻击 × 暴击期望 × 攻速（普攻口径）。health 按显示口径 ×10。"""
    page = CLASS_PAGE.get(str(class_id).lower())
    s = CLASS_STATS.get(page or "", {})
    grow = {k.lower(): v["base"] + v["per_level"] * level for k, v in s.items()}
    gflat = collections.defaultdict(float)
    gpct = collections.defaultdict(float)
    for item in (hero.get("equipment") or {}).values():
        if not item:
            continue
        enh = 1 + 0.05 * (item.get("enhancement") or 0)
        for a in item.get("affixes") or []:
            st = str(a.get("stat") or "").lower()
            val = float(a.get("value") or 0) * enh
            if st in PCT_STATS:
                gpct[st] += val / 100.0     # 100 raw = 1%
            else:
                gflat[st] += val
    tflat, tpct, tinc, tmore, tpen = _talent_bonus(hero.get("treeNodes") or [])

    def statv(name):
        key = name.lower()
        base = grow.get(key, 0) + gflat.get(key, 0) + tflat.get(key, 0)
        pct = gpct.get(key, 0) + tpct.get(key, 0) + tinc.get(key, 0)
        val = base * (1 + pct / 100.0)
        return val * (1 + tmore.get(key, 0) / 100.0) * (1 + tpen.get(key, 0) / 100.0)

    haste = gpct.get("haste", 0) + tpct.get("haste", 0) + tinc.get("haste", 0)
    cc = gpct.get("critchance", 0) + tpct.get("critchance", 0) + tinc.get("critchance", 0)
    cd = gpct.get("critdmg", 0) + tpct.get("critdmg", 0) + tinc.get("critdmg", 0)
    crit_avg = 1 + (cc / 100.0) * (1 + cd / 100.0)
    attack = statv("Attack")
    return {"dps": attack * crit_avg * (1 + haste / 100.0),
            "health": statv("Health") * 10}


def _xp_kept(hero_lv, stage_lv):
    """等级差经验保留比例（wiki leveling 口径）：关卡低于英雄 6 级以上快速衰减，最低 1%。"""
    below = stage_lv - hero_lv
    if below > 3:
        return max(1 - 0.99 * (below - 3) / 7.0, 0.01)
    over = hero_lv - stage_lv - 6
    if over > 0:
        return max(1.0 / (1.0 + (over / 2.0) ** 1.585), 0.01)
    return 1.0


def theory_rows(heroes):
    """按存档队伍算各关理论时薪（怪Lv ≤ 队伍最高等级+3）。
    经验/时 = 单杀经验 × 杀/时 × Σ各英雄等级差保留比例（队伍合计）；
    金币无等级差惩罚。返回 dict(dps, lv, rows) 或 None。"""
    if not heroes or not STAGES:
        return None
    ds = []
    for h in heroes:
        try:
            ds.append((_hero_dps(h.get("classId"), int(h.get("level") or 0), h),
                       int(h.get("level") or 0)))
        except Exception:
            pass
    if not ds:
        return None
    dps = sum(d["dps"] for d, _ in ds)
    if dps <= 0:
        return None
    max_lv = max(lv for _, lv in ds)
    min_hp = min(d["health"] for d, _ in ds)
    rows = []
    for mslug, mdata in STAGES.items():
        zh = mdata.get("zh") or mslug
        for st in mdata.get("stages") or []:
            if st["level"] > max_lv + 3:
                continue
            kills_h = 3600.0 / max(st["hp"] / dps, 0.2)
            kept = sum(_xp_kept(lv, st["level"]) for _, lv in ds)
            surv = (min_hp / st["boss_hit"]) if st["boss_hit"] else 99.0
            rows.append({"map": "%s %s · %s" % (zh, st["stage"],
                                                ("Veil", "Breach", "Abyss")[st.get("diff", 0)]),
                         "lv": int(st["level"]),
                         "xp_h": st["xp"] * kills_h * kept, "gold_h": st["gold"] * kills_h,
                         "kills_h": kills_h, "surv": surv})
    rows.sort(key=lambda r: -r["xp_h"])
    return {"dps": dps, "lv": max_lv, "rows": rows}


def tpl(name):
    if name not in _cache:
        _cache[name] = relook.imread_u(os.path.join(TPLDIR, name + ".png"))
    return _cache[name]


def match_center(img, name, th=0.85):
    """返回模板中心 (cx, cy, score)；未达阈值返回 None。"""
    t = tpl(name)
    r = cv2.matchTemplate(img, t, cv2.TM_CCOEFF_NORMED)
    mx = float(r.max())
    if mx < th:
        return None
    ys, xs = np.unravel_index(int(r.argmax()), r.shape)
    return int(xs + t.shape[1] / 2), int(ys + t.shape[0] / 2), mx


class Bot:
    """挂机引擎：被 GUI 线程启停，所有点击前都重新抓屏识图。"""

    def __init__(self, log):
        self.log = log
        self.stop_evt = threading.Event()
        self.enabled = {"chests": True, "craft": True, "stash": True}
        self.act_lo, self.act_hi = 1.5, 4.0
        self.rnd_lo, self.rnd_hi = 3.0, 8.0
        self.chest_mode = "左键"        # 左键 / 空格 / 右键
        self.hwnd = None

    # ---- 底层 ----
    def sleep(self, sec):
        self.stop_evt.wait(sec)

    def click_c(self, cx, cy, tag, right=False):
        sx, sy = recap.client_to_screen(self.hwnd, cx, cy)
        recap.activate(self.hwnd)
        time.sleep(0.15)
        if not recap.at_top(self.hwnd, sx, sy):
            recap.activate(self.hwnd)
            time.sleep(0.15)
        cur = rehand._get_cursor()
        rehand._travel(cur[0], cur[1], sx, sy)      # 贝塞尔+手抖+步时随机
        rehand.delay()                               # 100-200ms 真随机（含分神长尾）
        sp = rehand.rng.uniform(2.8, 4.2)            # 落点散布幅度也随机
        if right:
            rehand.rclick(sx, sy, spread=sp)
        else:
            rehand.click(sx, sy, spread=sp)
        self.log("      点 %-6s @ (%d,%d) 散布%.1f" % (tag, cx, cy, sp))

    # ---- 动作：收宝箱 ----
    def park_near_menu(self):
        """收箱前把鼠标停靠到菜单附近（随机方向 25-90px 偏移，只移动不点击）。"""
        img = recap.grab(self.hwnd)
        hit = match_center(img, "icon_menu", 0.85)
        if not hit:
            self.log("      菜单未找到，跳过停靠")
            return
        mx, my, s = hit
        ang = rehand.rng.uniform(0, 2 * math.pi)
        dist = rehand.rng.uniform(25, 90)
        px, py = int(mx + dist * math.cos(ang)), int(my + dist * math.sin(ang))
        sx, sy = recap.client_to_screen(self.hwnd, px, py)
        recap.activate(self.hwnd)
        time.sleep(0.15)
        cur = rehand._get_cursor()
        rehand._travel(cur[0], cur[1], sx, sy)   # 贝塞尔+手抖，到点即停
        self.log("      停靠菜单附近 (%d,%d) 偏距%.0f" % (px, py, dist))

    def _collect_at(self, cx, cy):
        """按收箱方式收一个宝箱：左键点击 / 空格 1-3 次 / 右键 1-3 次（次数与间隔真随机）。"""
        if self.chest_mode == "空格":
            sx, sy = recap.client_to_screen(self.hwnd, cx, cy)
            cur = rehand._get_cursor()
            rehand._travel(cur[0], cur[1], sx, sy)   # 鼠标扫过宝箱位置更像人
            for i in range(rehand.rng.randint(1, 3)):
                rehand.key(0x20)                      # SPACE
                self.log("      空格第%d次" % (i + 1))
                if i < 2:
                    self.sleep(rehand.rng.uniform(0.1, 0.5))
        elif self.chest_mode == "右键":
            for i in range(rehand.rng.randint(1, 3)):
                self.click_c(cx, cy, "右键箱", right=True)
                if i < 2:
                    self.sleep(rehand.rng.uniform(0.1, 0.5))
        else:
            self.click_c(cx, cy, "宝箱")

    def action_chests(self):
        self.park_near_menu()
        max_tries = rehand.rng.randint(6, 10)      # 尝试次数也随机
        got = 0
        for i in range(max_tries):
            img = recap.grab(self.hwnd)             # 每次尝试重新识别
            found = []
            for name in ("chest_wood", "chest_blue", "chest_gold"):
                t = tpl(name)
                r = cv2.matchTemplate(img, t, cv2.TM_CCOEFF_NORMED)
                ys, xs = np.where(r >= 0.78)
                for x, y in zip(xs, ys):
                    cx, cy = int(x + t.shape[1] / 2), int(y + t.shape[0] / 2)
                    # 12px 仅去匹配多峰（同一宝箱相邻命中），叠加宝箱间隔可小于宝箱尺寸，不能按距离豁免
                    if all(abs(cx - px) > 12 or abs(cy - py) > 12 for px, py, _ in found):
                        found.append((cx, cy, float(r[y, x])))
            if not found:
                self.log("      第%d次识别：无宝箱，结束" % (i + 1))
                break
            found.sort(key=lambda p: -p[2])
            for cx, cy, s in found[:3]:
                self.log("      宝箱 %.3f @ (%d,%d)" % (s, cx, cy))
                self._collect_at(cx, cy)
                got += 1
                rehand.delay()                       # 每箱间独立随机延迟
            self.sleep(rehand.rng.uniform(0.6, 1.4))  # 尝试间随机延迟（等动画）
        self.log("      收宝箱完成：共 %d 个（%s）" % (got, self.chest_mode))

    # ---- 动作：自动合成（每步重新 OCR，不用旧坐标）----
    def action_craft(self):
        steps = ((0, 0, "自动填入"), (CRAFT_DX, 0, "转化"), (CRAFT_BACK[0], CRAFT_BACK[1], "全部取回"))
        for i, (dx, dy, tag) in enumerate(steps):
            img = recap.grab(self.hwnd)
            hit = match_center(img, "btn_autofill", 0.90)
            if not hit:
                self.log("      工作台未开，跳过")
                return
            ax, ay, s = hit
            if i == 0:
                self.log("      自动填入 %.3f @ (%d,%d)" % (s, ax, ay))
            self.click_c(ax + dx, ay + dy, tag)
            self.sleep(rehand.rng.uniform(0.9, 1.6))

    # ---- 动作：一键存仓（仅按钮可用态=橙亮时才点）----
    def action_stash(self):
        img = recap.grab(self.hwnd)
        hit = match_center(img, "icon_stash", 0.95)
        if not hit:
            self.log("      存仓按钮不可用（背包空/仓库未开），跳过")
            return
        ax, ay, s = hit
        self.click_c(ax, ay, "存仓 %.3f" % s)

    # ---- 主循环 ----
    def run(self):
        try:
            hwnd, w, h = recap.find_game_window()
        except Exception as e:
            self.log("未找到游戏窗口：%r" % e)
            return
        self.hwnd = hwnd
        acts = []
        if self.enabled["chests"]:
            acts.append(("收宝箱", self.action_chests))
        if self.enabled["craft"]:
            acts.append(("自动合成", self.action_craft))
        if self.enabled["stash"]:
            acts.append(("一键存仓", self.action_stash))
        if not acts:
            self.log("未勾选任何功能")
            return
        self.log("挂机开始（%s）" % "、".join(a for a, _ in acts))
        rnd = 0
        while not self.stop_evt.is_set():
            rnd += 1
            self.log("— 第 %d 轮 —" % rnd)
            order = list(acts)
            rehand.rng.shuffle(order)                 # 每轮动作顺序随机
            for name, fn in order:
                if self.stop_evt.is_set():
                    break
                self.log("    [%s]" % name)
                try:
                    fn()
                except Exception as e:
                    self.log("    异常: %r" % e)
                self.sleep(rehand.rng.uniform(self.act_lo, self.act_hi))
            self.sleep(rehand.rng.uniform(self.rnd_lo, self.rnd_hi))
        self.log("挂机停止")


class SavePoller:
    """轮询 save.json（自动探测宿主/沙盒中最新的存档），纯本地读文件，零暴露。
    实测口径：XP/金币/击杀全部用存档增量计算；另随刷新重算理论表（theory_rows）。"""

    POLL = 10  # 默认轮询间隔（秒），UI 可改（self.poll）

    def __init__(self):
        self.lock = threading.Lock()
        self.samples = []            # [(t, s)]，s 为 _parse 结果，保留最近 30 分钟
        self.maps = {}               # (act,stage) -> {"hist_t","hist_xp","hist_gold","hist_kills","n"}
        self.cur = None              # 当前段图 key
        self.seg_t0 = 0.0            # 当前段开始时间
        self.seg_s0 = None           # 当前段起始样本
        self.session = {"xp": 0.0, "gold": 0.0, "kills": 0, "chests": 0, "lvups": 0}
        self.t0 = time.time()
        self.level = 0
        self.hero_xp = 0.0
        self.bestiary = {}
        self.theory = None            # 理论收益（theory_rows 结果，随存档刷新）
        self.path = ""
        self.mtime = 0.0
        self.stop_evt = threading.Event()
        self.poll = self.POLL          # 当前轮询间隔（秒），收益 Tab 可自定义
        self.err = ""

    # ---- 存档定位 ----
    def _find_save(self):
        cands = [os.path.expandvars(r"%APPDATA%\com.artxgames.questaway\save.json")]
        cands += glob.glob(r"D:\Sandbox\*\*\drive\C\Users\*\AppData\Roaming"
                           r"\com.artxgames.questaway\save.json")
        best = None
        for p in cands:
            try:
                m = os.path.getmtime(p)
            except OSError:
                continue
            if best is None or m > best[0]:
                best = (m, p)
        return best

    @staticmethod
    def _parse(path):
        d = json.load(open(path, encoding="utf-8"))
        g = d.get("game")
        if isinstance(g, str):
            g = json.loads(g)
        g = g.get("game", g)
        sim = g.get("sim") or {}
        heroes = g.get("heroes") or [{}]
        h0 = heroes[0] if heroes else {}
        chests = (g.get("contadores") or {}).get("bausAbertos") or {}
        heroes_out = []
        for h in heroes:
            heroes_out.append({"classId": h.get("classId"),
                               "level": int(h.get("level", 0) or 0),
                               "treeNodes": list(h.get("treeNodes") or []),
                               "equipment": h.get("equipment") or {}})
        return dict(
            gold=float(sim.get("gold", 0) or 0),
            xp=float(h0.get("xp", 0) or 0),
            level=int(h0.get("level", 0) or 0),
            act=int(g.get("act", 0) or 0),
            stage=int(g.get("stage", 0) or 0),
            kills=int(sim.get("kills", 0) or 0),
            chests=int(chests.get("normal", 0) or 0),
            bestiary=dict(sim.get("abatesDoBestiario") or {}),
            heroes=heroes_out,
        )

    # ---- 主循环 ----
    def loop(self):
        while not self.stop_evt.wait(self.poll):
            try:
                self.tick()
                self.err = ""
            except Exception as e:
                self.err = repr(e)

    def tick(self):
        found = self._find_save()
        if not found:
            return
        m, path = found
        if m == self.mtime:
            return                      # 存档未写盘，跳过
        s = self._parse(path)
        now = time.time()
        with self.lock:
            self.mtime, self.path = m, path
            self.level, self.hero_xp, self.bestiary = s["level"], s["xp"], s["bestiary"]
            self.theory = theory_rows(s["heroes"])
            prev = self.samples[-1][1] if self.samples else None
            self.samples.append((now, s))
            if self.samples and now - self.samples[0][0] > 1800:
                self.samples = self.samples[-900:]
            if prev is None:
                self._open_seg(now, s)
                return
            dt = now - prev[0] if False else now - self.samples[-2][0]
            if not (0 < dt < 120):
                self._open_seg(now, s)  # 间隔过大（暂停/重进），重开段防脏数据
                return
            # 会话累计
            self.session["xp"] += max(0.0, s["xp"] - prev["xp"])
            self.session["gold"] += max(0.0, s["gold"] - prev["gold"])
            self.session["kills"] += max(0, s["kills"] - prev["kills"])
            self.session["chests"] += max(0, s["chests"] - prev["chests"])
            if s["level"] > prev["level"]:
                self.session["lvups"] += s["level"] - prev["level"]
            # 图分段（覆盖当前段差值）
            key = (s["act"], s["stage"])
            if self.cur != key:
                self._close_seg()
                self._open_seg(now, s)
                key = self.cur
            d = self.maps[key]
            d["seg_t"] = now - self.seg_t0
            d["seg_xp"] = max(0.0, s["xp"] - self.seg_s0["xp"])
            d["seg_gold"] = max(0.0, s["gold"] - self.seg_s0["gold"])
            d["seg_kills"] = max(0, s["kills"] - self.seg_s0["kills"])

    def _open_seg(self, now, s):
        self.cur = (s["act"], s["stage"])
        self.seg_t0 = now
        self.seg_s0 = dict(s)
        d = self.maps.setdefault(self.cur, {"hist_t": 0.0, "hist_xp": 0.0,
                                            "hist_gold": 0.0, "hist_kills": 0,
                                            "n": 0, "seg_t": 0.0, "seg_xp": 0.0,
                                            "seg_gold": 0.0, "seg_kills": 0})
        d["n"] += 1
        d["seg_t"] = d["seg_xp"] = d["seg_gold"] = d["seg_kills"] = 0

    def _close_seg(self):
        if self.cur is None or self.seg_s0 is None:
            return
        d = self.maps[self.cur]
        d["hist_t"] += d.get("seg_t", 0.0)
        d["hist_xp"] += d.get("seg_xp", 0.0)
        d["hist_gold"] += d.get("seg_gold", 0.0)
        d["hist_kills"] += d.get("seg_kills", 0)

    # ---- 汇总输出（GUI 每 2 秒调）----
    def snapshot(self):
        with self.lock:
            now = time.time()
            # 实时速率：近 3 分钟窗口
            win = [x for x in self.samples if now - x[0] <= 180]
            xp_m = gold_m = kill_m = 0.0
            if len(win) >= 2:
                (ta, sa), (tb, sb) = win[0], win[-1]
                dt = tb - ta
                if dt > 0:
                    xp_m = max(0.0, sb["xp"] - sa["xp"]) / dt * 60
                    gold_m = max(0.0, sb["gold"] - sa["gold"]) / dt * 60
                    kill_m = max(0, sb["kills"] - sa["kills"]) / dt * 60
            # 升级 ETA（本级进度制：需 XP_TABLE[level-1] - xp）
            eta = None
            if 0 < self.level <= len(XP_TABLE):
                need = XP_TABLE[self.level - 1] - self.hero_xp
                if need > 0 and xp_m > 0:
                    eta = need / xp_m
            cur_map = None
            if self.cur:
                cur_map = (NAMES["maps"].get(str(self.cur[0] + 1), "第%d章" % (self.cur[0] + 1)),
                           self.cur[1])
            # 图排行
            rows = []
            for (a, st), d in self.maps.items():
                t = d["hist_t"] + d.get("seg_t", 0.0)
                if t < 60:
                    continue
                xp = d["hist_xp"] + d.get("seg_xp", 0.0)
                gold = d["hist_gold"] + d.get("seg_gold", 0.0)
                kl = d["hist_kills"] + d.get("seg_kills", 0)
                rows.append((NAMES["maps"].get(str(a + 1), "第%d章" % (a + 1)) + " %d-%d" % (a, st),
                             t, xp / t * 60, gold / t * 60, kl / t * 60, d["n"]))
            rows.sort(key=lambda r: -r[2])
            sess_dur = now - self.t0
            return dict(xp_m=xp_m, gold_m=gold_m, kill_m=kill_m, eta=eta,
                        level=self.level, hero_xp=self.hero_xp, cur_map=cur_map,
                        rows=rows, session=dict(self.session), dur=sess_dur,
                        theory=self.theory,
                        path=self.path, mtime=self.mtime, err=self.err,
                        n_samp=len(self.samples))


MARKET_NAMES = _load_json("_market_names.json") or {}   # 物品 id → 中文名（wiki 词库）
RARITY_ZH = {"common": "普通", "uncommon": "优良", "rare": "稀有",
             "heroic": "史诗", "ascendant": "传说", "legendary": "神话"}
SLOT_ZH = {"weapon": "武器", "offhand": "副手", "helmet": "头", "armor": "甲",
           "legs": "腿", "boots": "靴", "gloves": "手", "belt": "腰",
           "ring": "戒", "necklace": "链", "earrings": "耳", "bracelet": "镯", "amulet": "护"}
MARKET_API = "https://api.artxgames.com/questaway/market/listings?ordem=novos&porPagina=54&pagina={page}"
MARKET_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
             "Chrome/156.0.0.0 Safari/537.36 Edg/156.0.0.0")


class MarketWatcher:
    """跳蚤市场监控：mitmdump 抓包提令牌 → 直连 API 拉挂单 → 折扣筛漏。
    令牌只在游戏登录时经代理一次；之后监控直连（只读请求），不依赖游戏进程。"""

    def __init__(self, log):
        self.log = log
        self.lock = threading.Lock()
        self.stop_evt = threading.Event()
        self.token = ""
        self.core = "81313ae7534b"
        self.log_off = 0
        self.rows = []
        self.status = "未启动"
        self.err = ""
        self.thr = 80
        self.auto = True
        self.seen_ids = set()          # 已提醒过的挂单 id
        self.token_t = ""

    # ---- 抓包器与游戏启动 ----
    def ensure_sniffer(self):
        try:
            s = socket.socket()
            s.settimeout(0.6)
            s.connect(("127.0.0.1", 8080))
            s.close()
            return True, "抓包器已在运行"
        except OSError:
            s.close()
        script = os.path.join(CAPTURE_DIR, "qa_sniff.py")
        if not os.path.exists(script):
            return False, "缺少 %s" % script
        try:
            subprocess.Popen(["mitmdump", "-s", script, "-p", "8080", "--set", "flow_detail=0"],
                             creationflags=0x00000008,   # DETACHED_PROCESS
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(1.5)
            s = socket.socket(); s.settimeout(0.6)
            s.connect(("127.0.0.1", 8080)); s.close()
            return True, "抓包器已启动"
        except FileNotFoundError:
            return False, "未安装 mitmproxy（命令行执行: pip install mitmproxy）"
        except OSError:
            return False, "抓包器启动失败"

    def launch_game(self, exe_path):
        env = dict(os.environ)
        env["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = "--proxy-server=127.0.0.1:8080"
        subprocess.Popen([exe_path], env=env, creationflags=0x00000008)
        self.log("游戏已带代理启动，请登录")

    # ---- 令牌监听 ----
    def _load_token(self):
        try:
            self.token = open(TOKEN_FILE, encoding="utf-8").read().strip()
        except OSError:
            pass

    def watch_tokens(self):
        self._load_token()
        if self.token:
            self.status = "令牌已从缓存加载"
        logf = os.path.join(CAPTURE_DIR, "flows.log")
        while not self.stop_evt.wait(2):
            try:
                if not os.path.exists(logf):
                    continue
                size = os.path.getsize(logf)
                if self.log_off > size:
                    self.log_off = 0
                if size == self.log_off:
                    continue
                with open(logf, "rb") as f:
                    f.seek(self.log_off)
                    data = f.read().decode("utf-8", "replace")
                    self.log_off = f.tell()
                hits = re.findall(r'"authorization": "Bearer ([A-Za-z0-9_\-]{16,})"', data)
                if hits and hits[-1] != self.token:
                    self.token = hits[-1]
                    self.token_t = time.strftime("%H:%M:%S")
                    try:
                        open(TOKEN_FILE, "w", encoding="utf-8").write(self.token)
                    except OSError:
                        pass
                    self.log("市场监控：捕获新令牌 %s…" % self.token[:8])
                    self.status = "令牌已更新"
            except Exception as e:
                self.err = repr(e)

    # ---- 市场拉取 ----
    def _fetch(self, page):
        req = urllib.request.Request(MARKET_API.format(page=page), headers={
            "authorization": "Bearer " + self.token,
            "x-core-version": self.core,
            "user-agent": MARKET_UA,
            "origin": "http://tauri.localhost",
            "referer": "http://tauri.localhost/",
            "accept": "*/*",
            "accept-encoding": "gzip"})
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read()
            if raw[:2] == b"\x1f\x8b":
                raw = gzip.decompress(raw)
        return json.loads(raw.decode("utf-8"))

    @staticmethod
    def _name(it):
        base = it.get("baseType")
        iid = str(it.get("id", ""))
        if base:
            return MARKET_NAMES.get(base, base)
        zh = MARKET_NAMES.get(iid)          # 材料/宝石/符文：id 即类型名
        if zh:
            return zh
        if iid.startswith(("it_", "mk_")):   # 随机装备无 baseType：组合显示
            rar = RARITY_ZH.get(it.get("rarity", ""), "装备")
            slot = SLOT_ZH.get(it.get("slot", ""), "")
            lv = it.get("itemLevel", "")
            name = "%s·%s·Lv%s" % (rar, slot, lv) if slot else "%s·Lv%s" % (rar, lv)
            return name
        if iid == "0" or it.get("pilha") == "rift":
            return "裂隙物品"
        return iid or "?"

    @staticmethod
    def _parse(ads):
        rows = []
        for a in ads:
            it = a.get("item") or {}
            qtd = it.get("qtd", 1) or 1
            preco, ref = a.get("preco"), a.get("referencia")
            disc = round(100.0 * preco / ref) if (ref and preco) else None
            if it.get("pilha") and not it.get("rarity"):
                rar = "材料"
            else:
                rar = RARITY_ZH.get(it.get("rarity", ""), "材料")
            slot = SLOT_ZH.get(it.get("slot", ""), "")
            rows.append({
                "name": MarketWatcher._name(it) + (" x%d" % qtd if qtd > 1 else ""),
                "rar": rar + (("·" + slot) if slot else ""),
                "lv": it.get("itemLevel", ""),
                "preco": preco, "ref": ref or "", "disc": disc,
                "id": a.get("id"),
            })
        rows.sort(key=lambda r: r["disc"] if r["disc"] is not None else 9999)
        return rows

    def poll_market(self, on_bargain):
        while not self.stop_evt.wait(10):
            if not self.token or not self.auto:
                with self.lock:
                    if not self.token:
                        self.status = "等待令牌（登录游戏自动捕获）"
                continue
            try:
                ads = []
                for p in range(1, 5):
                    d = self._fetch(p)
                    batch = d.get("anuncios", [])
                    ads += batch
                    if len(batch) < 54:
                        break
                rows = self._parse(ads)
                news = []
                with self.lock:
                    self.rows = rows
                    self.err = ""
                    self.status = "共 %d 条 · %s" % (len(rows), time.strftime("%H:%M:%S"))
                    for r in rows:
                        if r["disc"] is not None and r["disc"] < self.thr and r["id"] not in self.seen_ids:
                            self.seen_ids.add(r["id"])
                            news.append(r)
                    if len(self.seen_ids) > 4000:
                        self.seen_ids = set(list(self.seen_ids)[-2000:])
                if news:
                    on_bargain(news)
            except urllib.error.HTTPError as e:
                try:
                    body = e.read().decode("utf-8", "replace")[:120]
                except Exception:
                    body = ""
                with self.lock:
                    if e.code == 409 or "session" in body:
                        self.status = "令牌失效（游戏重新登录顶掉旧会话），等待新令牌…"
                    else:
                        self.status = "HTTP %s %s" % (e.code, body)
            except Exception as e:
                with self.lock:
                    self.err = repr(e)
                    self.status = "错误: %r" % e

    def snapshot(self):
        with self.lock:
            return dict(rows=list(self.rows), status=self.status, token=self.token,
                        token_t=self.token_t, err=self.err)


class App:
    def __init__(self, root):
        self.root = root
        root.title("QA助手 RE")
        root.attributes("-topmost", True)
        root.resizable(False, False)
        self.q = queue.Queue()
        self.worker = None

        pad = dict(padx=8, pady=4)
        nb = ttk.Notebook(root)
        nb.pack(fill="both", expand=True, **pad)
        tab1 = ttk.Frame(nb)
        tab2 = ttk.Frame(nb)
        tab3 = ttk.Frame(nb)
        if MARKET_BUILD:
            nb.add(tab3, text="市场监控")
        nb.add(tab1, text="挂机")
        nb.add(tab2, text="收益分析")

        top = ttk.Frame(tab1)
        top.pack(fill="x", **pad)

        ttk.Label(top, text="功能：").pack(side="left")
        self.v_chests = tk.BooleanVar(value=True)
        self.v_craft = tk.BooleanVar(value=True)
        self.v_stash = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text="收宝箱", variable=self.v_chests).pack(side="left")
        ttk.Checkbutton(top, text="自动合成", variable=self.v_craft).pack(side="left")
        ttk.Checkbutton(top, text="一键存仓", variable=self.v_stash).pack(side="left")
        ttk.Label(top, text="   收箱方式：").pack(side="left")
        self.v_mode = tk.StringVar(value="左键")
        ttk.Combobox(top, textvariable=self.v_mode, values=("左键", "空格", "右键"),
                     width=6, state="readonly").pack(side="left")

        # ---- 重要提醒（红字置顶显示）----
        warn = ttk.LabelFrame(tab1, text="⚠ 重要提醒（必读）")
        warn.pack(fill="x", **pad)
        for i, line in enumerate((
                "开启【一键存仓】【自动合成】前，必须先在游戏里打开面板：仓库 + 转化台！",
                "必须 2560×1440 全屏（窗口 1X / 战场 1X / 界面 1.1x / 物品 100%），否则点击会偏！")):
            ttk.Label(warn, text="● " + line, foreground="#d00",
                      font=("Microsoft YaHei", 10, "bold")).grid(row=i, column=0, sticky="w", padx=6, pady=2)

        frm = ttk.LabelFrame(tab1, text="随机间隔（秒，区间自定义）")
        frm.pack(fill="x", **pad)
        ttk.Label(frm, text="动作间隔").grid(row=0, column=0, sticky="w", padx=4, pady=2)
        self.e_alo = ttk.Spinbox(frm, from_=0, to=3600, width=7, increment=0.5)
        self.e_ahi = ttk.Spinbox(frm, from_=0, to=3600, width=7, increment=0.5)
        self.e_alo.set(4.0)
        self.e_ahi.set(12.0)
        ttk.Label(frm, text="每轮间隔").grid(row=1, column=0, sticky="w", padx=4, pady=2)
        self.e_rlo = ttk.Spinbox(frm, from_=0, to=3600, width=7, increment=0.5)
        self.e_rhi = ttk.Spinbox(frm, from_=0, to=3600, width=7, increment=0.5)
        self.e_rlo.set(20.0)
        self.e_rhi.set(360.0)
        self.e_alo.grid(row=0, column=1, padx=2)
        ttk.Label(frm, text="~").grid(row=0, column=2)
        self.e_ahi.grid(row=0, column=3, padx=2)
        ttk.Label(frm, text="秒").grid(row=0, column=4, sticky="w")
        self.e_rlo.grid(row=1, column=1, padx=2)
        ttk.Label(frm, text="~").grid(row=1, column=2)
        self.e_rhi.grid(row=1, column=3, padx=2)
        ttk.Label(frm, text="秒").grid(row=1, column=4, sticky="w")

        btns = ttk.Frame(tab1)
        btns.pack(fill="x", **pad)
        self.b_start = ttk.Button(btns, text="开始挂机", command=self.start)
        self.b_start.pack(side="left", expand=True, fill="x", padx=4)
        self.b_stop = ttk.Button(btns, text="停止", command=self.stop, state="disabled")
        self.b_stop.pack(side="left", expand=True, fill="x", padx=4)

        self.txt = tk.Text(tab1, height=12, width=64, font=("Consolas", 9), state="disabled")
        self.txt.pack(fill="both", expand=True, **pad)

        info = ttk.LabelFrame(tab1, text="说明")
        info.pack(fill="x", **pad)
        for i, line in enumerate((
                "分辨率：需 2560×1440 全屏（窗口 1X / 战场 1X / 界面 1.1x / 物品 100%），分辨率不符会点偏。",
                "实现：PrintWindow 抓屏 + OpenCV 模板匹配识图（每步先识图再点）+ SendInput 物理点击，无注入无调试端口。",
                "推荐间隔：动作间隔 4~12 秒，每轮间隔 20~360 秒。点击为物理输入，挂机期间会占用鼠标；窗口保持非最小化。")):
            ttk.Label(info, text=line, foreground="#555").grid(row=i, column=0, sticky="w", padx=6, pady=1)

        # ---- 收益分析 Tab ----
        self.lbl_save = ttk.Label(tab2, text="存档：等待首次读取…", foreground="#555")
        self.lbl_save.pack(fill="x", **pad)
        sr = ttk.Frame(tab2)
        sr.pack(fill="x", **pad)
        ttk.Label(sr, text="存档刷新间隔(秒):").pack(side="left")
        self.e_poll = ttk.Spinbox(sr, from_=2, to=600, width=5, increment=1,
                                  command=self._set_poll)
        self.e_poll.set(int(self.poller.poll) if hasattr(self, "poller") else 10)
        self.e_poll.pack(side="left", padx=(4, 0))
        self.e_poll.bind("<Return>", lambda e: self._set_poll())
        self.e_poll.bind("<FocusOut>", lambda e: self._set_poll())
        ttk.Label(sr, text="（实测与理论表都按这个间隔刷新，2~600）",
                  foreground="#555").pack(side="left", padx=(6, 0))
        rt = ttk.LabelFrame(tab2, text="实时收益（近 3 分钟窗口，存档增量实测）")
        rt.pack(fill="x", **pad)
        self.v_cur = tk.StringVar(value="—")
        self.v_xpm = tk.StringVar(value="—")
        self.v_xph = tk.StringVar(value="—")
        self.v_gpm = tk.StringVar(value="—")
        self.v_gph = tk.StringVar(value="—")
        self.v_kpm = tk.StringVar(value="—")
        self.v_eta = tk.StringVar(value="—")
        for r, (name, var) in enumerate((
                ("当前地图", self.v_cur), ("经验/分", self.v_xpm), ("经验/时", self.v_xph),
                ("金币/分", self.v_gpm), ("金币/时", self.v_gph), ("击杀/分", self.v_kpm))):
            ttk.Label(rt, text=name).grid(row=r // 2 * 2, column=(r % 2) * 2, sticky="w", padx=6, pady=2)
            ttk.Label(rt, textvariable=var, font=("Consolas", 10, "bold")).grid(
                row=r // 2 * 2 + 1, column=(r % 2) * 2, sticky="w", padx=6)
        self.lbl_eta2 = ttk.Label(tab2, textvariable=self.v_eta, foreground="#0a7")
        self.lbl_eta2.pack(fill="x", **pad)

        theory = ttk.LabelFrame(tab2, text="理论最佳关卡（队伍DPS×关卡表，普攻口径未含技能/走位；经验=队伍合计，实测仅首英雄）")
        theory.pack(fill="x", **pad)
        tcols = ("tmap", "tlv", "tks", "txp", "tgd", "tsv")
        self.thtree = ttk.Treeview(theory, columns=tcols, show="headings", height=5)
        for cid, txt, w, anc in (("tmap", "关卡", 120, "w"), ("tlv", "怪Lv", 45, "e"),
                                 ("tks", "杀/时", 70, "e"), ("txp", "经验/时", 85, "e"),
                                 ("tgd", "金币/时", 85, "e"), ("tsv", "生存", 70, "e")):
            self.thtree.heading(cid, text=txt)
            self.thtree.column(cid, width=w, anchor=anc)
        self.thtree.pack(side="left", fill="both", expand=True, padx=4, pady=4)
        self.thtree.tag_configure("risk", background="#ffdcdc")
        tbtn = ttk.Frame(theory)
        tbtn.pack(side="left", fill="y", padx=4)
        self.sort_theory = "xp"
        ttk.Button(tbtn, text="按经验排", width=9, command=lambda: self._sort_theory("xp")).pack(pady=2)
        ttk.Button(tbtn, text="按金币排", width=9, command=lambda: self._sort_theory("gold")).pack(pady=2)
        self.lbl_theory = ttk.Label(tbtn, text="等待存档…", foreground="#555", justify="center")
        self.lbl_theory.pack(pady=4)
        self._last_theory = None

        rank = ttk.LabelFrame(tab2, text="地图收益排行（每图实测，同一图多次进入自动合并）")
        rank.pack(fill="both", expand=True, **pad)
        cols = ("map", "t", "xpm", "gpm", "kpm", "n")
        self.tree = ttk.Treeview(rank, columns=cols, show="headings", height=7)
        for cid, txt, w in (("map", "地图", 130), ("t", "时长", 70), ("xpm", "经验/分", 80),
                            ("gpm", "金币/分", 80), ("kpm", "击杀/分", 80), ("n", "进入", 50)):
            self.tree.heading(cid, text=txt)
            self.tree.column(cid, width=w, anchor="w")
        self.tree.pack(side="left", fill="both", expand=True, padx=4, pady=4)
        rbtn = ttk.Frame(rank)
        rbtn.pack(side="left", fill="y", padx=4)
        self.sort_key = 2  # 0=地图 1=时长 2=经验 3=金币 4=击杀
        ttk.Button(rbtn, text="按经验排", width=9, command=lambda: self._sort_rank(2)).pack(pady=2)
        ttk.Button(rbtn, text="按金币排", width=9, command=lambda: self._sort_rank(3)).pack(pady=2)
        ttk.Button(rbtn, text="按击杀排", width=9, command=lambda: self._sort_rank(4)).pack(pady=2)

        self.lbl_sess = ttk.Label(tab2, text="会话累计：—", foreground="#555")
        self.lbl_sess.pack(fill="x", **pad)

        # 存档轮询线程（与挂机无关，启动即统计）
        self.poller = SavePoller()
        threading.Thread(target=self.poller.loop, daemon=True).start()
        self._last_rows = []
        root.after(2000, self._refresh_income)

        # ---- 市场监控 Tab ----
        mk_top = ttk.Frame(tab3)
        mk_top.pack(fill="x", **pad)
        ttk.Label(mk_top, text="游戏路径:").pack(side="left")
        self.e_game = ttk.Entry(mk_top, width=44)
        self.e_game.pack(side="left", padx=4)
        self.e_game.insert(0, GAME_EXE_DEFAULT)
        ttk.Button(mk_top, text="启动游戏(带抓包)", command=self._launch_game).pack(side="left", padx=4)
        ttk.Button(mk_top, text="重连抓包器", command=self._ensure_sniffer).pack(side="left", padx=4)

        mk_ctl = ttk.Frame(tab3)
        mk_ctl.pack(fill="x", **pad)
        ttk.Label(mk_ctl, text="捡漏阈值%:").pack(side="left")
        self.e_thr = ttk.Spinbox(mk_ctl, from_=10, to=99, width=4)
        self.e_thr.set(80)
        self.e_thr.pack(side="left", padx=(2, 10))
        self.v_mkauto = tk.BooleanVar(value=True)
        ttk.Checkbutton(mk_ctl, text="自动监控(10s)", variable=self.v_mkauto,
                        command=self._set_mkauto).pack(side="left", padx=4)
        ttk.Button(mk_ctl, text="立即刷新", command=self._mk_refresh_once).pack(side="left", padx=4)
        self.lbl_mkt = ttk.Label(mk_ctl, text="状态：未启动", foreground="#555")
        self.lbl_mkt.pack(side="right")

        mkfrm = ttk.LabelFrame(tab3, text="跳蚤市场挂单（红=低于阈值捡漏，黄=95折以下）")
        mkfrm.pack(fill="both", expand=True, **pad)
        mcols = ("mname", "mrar", "mlv", "mpreco", "mref", "mdisc")
        self.mktree = ttk.Treeview(mkfrm, columns=mcols, show="headings", height=12)
        for cid, txt, w, anc in (("mname", "物品", 200, "w"), ("mrar", "类型", 100, "w"),
                                 ("mlv", "Lv", 45, "e"), ("mpreco", "价格", 80, "e"),
                                 ("mref", "参考价", 80, "e"), ("mdisc", "折扣", 60, "e")):
            self.mktree.heading(cid, text=txt)
            self.mktree.column(cid, width=w, anchor=anc)
        self.mktree.tag_configure("bargain", background="#ffdcdc")
        self.mktree.tag_configure("good", background="#fff6d5")
        mvs = ttk.Scrollbar(mkfrm, orient="vertical", command=self.mktree.yview)
        self.mktree.configure(yscrollcommand=mvs.set)
        self.mktree.pack(side="left", fill="both", expand=True, padx=(4, 0), pady=4)
        mvs.pack(side="left", fill="y", pady=4)

        if MARKET_BUILD:
            self.watcher = MarketWatcher(self.log)
            threading.Thread(target=self.watcher.watch_tokens, daemon=True).start()
            threading.Thread(target=self.watcher.poll_market, args=(self._on_bargain,), daemon=True).start()
            root.after(3000, self._refresh_market)

        root.after(100, self._pump)

    def log(self, s):
        self.q.put(s)

    def _set_poll(self):
        """存档刷新间隔自定义（2~600 秒），立即生效。"""
        try:
            v = max(2.0, min(600.0, float(str(self.e_poll.get()).strip())))
        except (ValueError, TypeError):
            return
        self.e_poll.set(("%g" % v) if v != int(v) else str(int(v)))
        if hasattr(self, "poller"):
            self.poller.poll = v

    def _pump(self):
        try:
            while True:
                s = self.q.get_nowait()
                self.txt.configure(state="normal")
                self.txt.insert("end", s + "\n")
                self.txt.see("end")
                self.txt.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(100, self._pump)

    # ---- 收益分析刷新 ----
    @staticmethod
    def _fmt_dur(sec):
        sec = int(sec)
        return "%dh%02dm" % (sec // 3600, sec % 3600 // 60) if sec >= 3600 else "%dm%02ds" % (sec // 60, sec % 60)

    def _refresh_income(self):
        try:
            p = self.poller.snapshot()
            if p["path"]:
                short = p["path"]
                if "Sandbox" in short:
                    short = "沙盒存档 " + short.split("drive\\")[-1].split("\\")[-2]
                else:
                    short = "宿主存档"
                t = time.strftime("%H:%M:%S", time.localtime(p["mtime"]))
                self.lbl_save.configure(text="存档：%s · 写盘 %s · 样本 %d 个%s" % (
                    short, t, p["n_samp"], (" · 错误 " + p["err"]) if p["err"] else ""))
            self.v_cur.set("%s 第%d关" % p["cur_map"] if p["cur_map"] else "—")
            self.v_xpm.set("%.1f" % p["xp_m"])
            self.v_xph.set("%.0f" % (p["xp_m"] * 60))
            self.v_gpm.set("%.1f" % p["gold_m"])
            self.v_gph.set("%.0f" % (p["gold_m"] * 60))
            self.v_kpm.set("%.1f" % p["kill_m"])
            if p["eta"] is not None:
                self.v_eta.set("Lv%d → Lv%d：还差 %.0f 经验 ≈ %s（按当前实测速率）" % (
                    p["level"], p["level"] + 1, XP_TABLE[p["level"] - 1] - p["hero_xp"],
                    self._fmt_dur(p["eta"] * 60)))
            else:
                self.v_eta.set("Lv%s 升级估算：等待更多样本（需要 3 分钟内有经验增量）" % p["level"])
            s = p["session"]
            self.lbl_sess.configure(text="会话累计：%s · 经验 %.0f · 金币 %.0f · 击杀 %d · 宝箱 %d · 升级 %d 次" % (
                self._fmt_dur(p["dur"]), s["xp"], s["gold"], s["kills"], s["chests"], s["lvups"]))
            self._last_rows = p["rows"]
            self._fill_tree()
            th = p.get("theory")
            self._last_theory = th
            if th and th.get("rows"):
                self.lbl_theory.configure(text="队伍DPS %.0f\n限≤Lv%d+3" % (th["dps"], th["lv"]))
            else:
                self.lbl_theory.configure(text="等待存档…")
            self._fill_theory()
        except Exception:
            pass
        self.root.after(2000, self._refresh_income)

    # ---- 市场监控 ----
    def _launch_game(self):
        ok, msg = self.watcher.ensure_sniffer()
        self.log("抓包器：" + msg)
        self.watcher.launch_game(self.e_game.get().strip())

    def _ensure_sniffer(self):
        ok, msg = self.watcher.ensure_sniffer()
        self.log("抓包器：" + msg)

    def _set_mkauto(self):
        self.watcher.auto = self.v_mkauto.get()
        self.watcher.thr = float(self.e_thr.get() or 80)

    def _mk_refresh_once(self):
        self.watcher.thr = float(self.e_thr.get() or 80)
        threading.Thread(target=self._mk_once, daemon=True).start()

    def _mk_once(self):
        """立即强制拉一轮（无视 auto 开关）。"""
        w = self.watcher
        if not w.token:
            self.log("市场监控：无令牌，先登录游戏")
            return
        try:
            ads = []
            for pg in range(1, 5):
                d = w._fetch(pg)
                batch = d.get("anuncios", [])
                ads += batch
                if len(batch) < 54:
                    break
            with w.lock:
                w.rows = w._parse(ads)
                w.status = "共 %d 条（手动）· %s" % (len(w.rows), time.strftime("%H:%M:%S"))
        except Exception as e:
            with w.lock:
                w.status = "错误: %r" % e

    def _on_bargain(self, news):
        try:
            import winsound
            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        except Exception:
            pass
        for r in news[:5]:
            self.log("捡漏：%s %s → %d 金（参考 %s，%d%%）" % (
                r["name"], r["rar"], r["preco"], r["ref"], r["disc"]))

    def _refresh_market(self):
        try:
            p = self.watcher.snapshot()
            self.lbl_mkt.configure(text="状态：%s%s" % (p["status"],
                ("（令牌 %s… @%s）" % (p["token"][:8], p["token_t"])) if p["token"] else ""))
            thr = float(self.e_thr.get() or 80)
            self.mktree.delete(*self.mktree.get_children())
            for r in p["rows"]:
                tag = ""
                if r["disc"] is not None:
                    if r["disc"] < thr:
                        tag = "bargain"
                    elif r["disc"] < 95:
                        tag = "good"
                self.mktree.insert("", "end", tags=(tag,), values=(
                    r["name"], r["rar"], r["lv"], r["preco"], r["ref"],
                    ("%d%%" % r["disc"]) if r["disc"] is not None else "-"))
        except Exception:
            pass
        self.root.after(3000, self._refresh_market)

    def _sort_rank(self, key):
        self.sort_key = key
        self._fill_tree()

    def _sort_theory(self, key):
        self.sort_theory = key
        self._fill_theory()

    def _fill_theory(self):
        th = self._last_theory
        self.thtree.delete(*self.thtree.get_children())
        if not th or not th.get("rows"):
            return
        key = self.sort_theory
        rows = sorted(th["rows"], key=lambda r: -r[key + "_h"])
        for r in rows[:12]:
            tag = ("risk",) if r["surv"] < 5 else ()
            self.thtree.insert("", "end", tags=tag, values=(
                r["map"], r["lv"], "%.0f" % r["kills_h"], "%.0f" % r["xp_h"],
                "%.0f" % r["gold_h"], "%.0f击" % r["surv"] if r["surv"] < 99 else "—"))

    def _fill_tree(self):
        rows = sorted(self._last_rows, key=lambda r: -r[self.sort_key])
        self.tree.delete(*self.tree.get_children())
        for r in rows:
            self.tree.insert("", "end", values=(
                r[0], self._fmt_dur(r[1]), "%.0f" % r[2], "%.0f" % r[3], "%.1f" % r[4], r[5]))

    def _read_interval(self, lo_e, hi_e, name):
        lo = float(lo_e.get())
        hi = float(hi_e.get())
        if lo < 0 or hi < 0 or lo > hi:
            raise ValueError("%s 区间无效（需 0 ≤ 下限 ≤ 上限）" % name)
        return lo, hi

    def start(self):
        try:
            a_lo, a_hi = self._read_interval(self.e_alo, self.e_ahi, "动作间隔")
            r_lo, r_hi = self._read_interval(self.e_rlo, self.e_rhi, "每轮间隔")
        except ValueError as e:
            messagebox.showwarning("QA助手 RE", str(e))
            return
        b = Bot(self.log)
        b.enabled = {"chests": self.v_chests.get(), "craft": self.v_craft.get(), "stash": self.v_stash.get()}
        b.chest_mode = self.v_mode.get()
        b.act_lo, b.act_hi, b.rnd_lo, b.rnd_hi = a_lo, a_hi, r_lo, r_hi
        self.worker = b
        self.b_start.configure(state="disabled")
        self.b_stop.configure(state="normal")
        threading.Thread(target=b.run, daemon=True).start()

    def stop(self):
        if self.worker:
            self.worker.stop_evt.set()
        self.b_start.configure(state="normal")
        self.b_stop.configure(state="disabled")


def main():
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
