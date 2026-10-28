# -*- coding: utf-8 -*-
"""QuestAway 战斗仿真（口径：wiki combat/leveling/chests/economy 公式文档，客户端 1.0.4-1.0.5）。
复刻 farming 计算器：每关 杀/经验/金币/宝箱 每小时、关卡评价、每英雄升级时间、
通关用时校准、离线收益。技能循环未逐个建模（wiki 亦标注 40 技能有脚本特例），
以普攻+DoT+全词缀/天赋/护甲/抗性/闪避/格挡/吸收/等级差/怒气 全链路仿真，
整体节奏用「通关用时校准」对齐实战。
"""
import json, math, os, re

_DIR = os.path.dirname(os.path.abspath(__file__))

def _load(name):
    try:
        return json.load(open(os.path.join(_DIR, name), encoding="utf-8"))
    except Exception:
        return None

STAGES = _load("_stages.json") or {}
CLASS_STATS = _load("_class_stats.json") or {}
TREE = _load("_tree.json") or {}
XP_TABLE = _load("_xp_table.json") or []

HEAVY = {"warrior", "barbarian", "paladin"}
LIGHT = {"mage", "healer", "witch"}
ACC1 = {"mage", "healer", "witch"}
WITCH = {"witch"}

# 百分比型词缀（存档按基点存：100 = 1%）
PCT_STATS = {"critChance", "critDamage", "haste", "castSpeed", "cdr", "penetration",
             "elemPen", "goldFind", "weaken", "curse", "expose", "fireResist",
             "iceResist", "lightningResist", "chaosResist", "allResist",
             "elemDamagePct", "attackPct"}
# 评级型（原值用）
RATING_STATS = {"dodge", "block", "elemBlock", "absorption", "accuracy", "thorns", "regen"}

def _class_weight(cls):
    if cls in HEAVY: return 1.0
    if cls in LIGHT: return 0.6
    return 0.8

def _armor(L):
    """怪物护甲（文档仅一点：L30 ≈ 515 → 削 58.8%）。按 1.12×(100+12L) 拟合。"""
    return 1.12 * (100 + 12 * L)

def _monster_cut(L, hero_level, pen=0.0):
    """怪物护甲对英雄物理伤害的削减（按攻击方=英雄等级）。"""
    a = _armor(L)
    return min(0.85, 0.85 * a / (a + 50 + 6 * hero_level)) * (1 - pen)

def _gap_factor(mon_l, hero_l, boss=False):
    gap = max(0, mon_l - hero_l)
    if boss:
        f = 1.15 ** min(gap, 3)
        if gap > 3:
            f *= 3 ** (gap - 3)
        return f
    f = 1.15 ** min(gap, 5)
    if gap > 5:
        f *= 1.6 ** (gap - 5)
    return f

def _dodge_keep(gap):
    return max(0.0, 1.0 - 0.1 * gap)

def _early_scale(stage_l):
    """17 级以下怪群伤害缩水（combat.md 表）。"""
    tbl = [(1, 0.027), (5, 0.166), (8, 0.375), (10, 0.514), (13, 0.722), (15, 0.861), (17, 1.0)]
    if stage_l >= 17:
        return 1.0
    for (a, va), (b, vb) in zip(tbl, tbl[1:]):
        if a <= stage_l <= b:
            return va + (vb - va) * (stage_l - a) / (b - a)
    return 0.027

def _tree_effect(nodes):
    """(flat, pct, more, penalty) 聚合：flat 相加、pct 相加、more 连乘、penalty 相加。"""
    flat, pct, more, pen = {}, {}, {}, {}
    eff = (TREE or {}).get("effects") or {}
    keystone = (TREE or {}).get("keystones") or {}
    for nid in nodes or []:
        e = eff.get(str(nid))
        if not e:
            continue
        for stat, kind, val in e:
            if kind == "flat":
                flat[stat] = flat.get(stat, 0) + val
            elif kind == "pct":
                pct[stat] = pct.get(stat, 0) + val
        ks = keystone.get(str(nid))
        if ks:
            for stat, val in (ks.get("more") or {}).items():
                more[stat] = more.get(stat, 1.0) * (1 + val / 100.0)
            for stat, val in (ks.get("penalty") or {}).items():
                pen[stat] = pen.get(stat, 0) + val
    return flat, pct, more, pen

def hero_sheet(h):
    """存档英雄 → 完整面板（combat.md 六步构建）。"""
    cls = (h.get("classId") or "").lower()
    lvl = int(h.get("level") or 0)
    eq = h.get("equipment") or {}
    flat, pct, more, pen = _tree_effect(h.get("treeNodes"))
    s = {k: 0.0 for k in ("attack", "defense", "health", "haste", "critChance", "critDamage",
                          "castSpeed", "cdr", "penetration", "elemPen", "accuracy", "dodge",
                          "block", "elemBlock", "absorption", "regen", "lifesteal", "thorns",
                          "goldFind", "weaken", "chill", "blind", "curse", "expose",
                          "elemDamage", "bleed", "decay", "burn", "poison",
                          "fireResist", "iceResist", "lightningResist", "chaosResist",
                          "lifestealPct", "healDone", "healRecv", "furyPct", "maxResist")}
    s["class"], s["level"] = cls, lvl
    # 1) 装备词缀 + 宝石（宝石按 1 颗 T1 计已并入 sockets 的 id，不重复计值）
    for slot, it in eq.items():
        if not it or not isinstance(it, dict):
            continue
        for a in it.get("affixes") or []:
            stat, val = a.get("stat"), float(a.get("value") or 0)
            if stat in PCT_STATS:
                val = val / 100.0          # 基点 → 分数
            if stat in s:
                s[stat] += val
            elif stat == "elemDamage":
                s["elemDamage"] += val
        for gid in it.get("sockets") or []:
            pass  # 宝石数值未建模（只影响加成估计），符文之语窗口另算
    # 2) 职业成长 × 等级  3) 职业基础
    cs = CLASS_STATS.get(cls) or {}
    for stat, dv in cs.items():
        base, per = float(dv.get("base") or 0), float(dv.get("per_level") or 0)
        s[stat.lower()] = s.get(stat.lower(), 0) + base + per * max(0, lvl - 1)
    # Witch：混乱伤害替代攻击成长（简化：攻击成长不动，混乱并入 elemDamage 处理跳过）
    # 4) 基础命中
    s["accuracy"] += round((100 + 12 * lvl) * (1.0 if cls in ACC1 else 1.5))
    # Healer：+15% 攻击的 Regen（最终缩放前）
    if cls == "healer":
        s["regen"] += 0.15 * s["attack"]
    # 5) 天赋：flat → ×(1+pct) → ×more → −penalty；allResist 分摊
    for stat in [k for k in s if isinstance(s[k], (int, float))]:
        f, p = flat.get(stat, 0), pct.get(stat, 0)
        v = (s[stat] + f) * (1 + p / 100.0) * more.get(stat, 1.0) - pen.get(stat, 0)
        s[stat] = max(0.0, v)
    ar_f, ar_p = flat.get("allResist", 0), pct.get("allResist", 0)
    for r in ("fireResist", "iceResist", "lightningResist", "chaosResist"):
        s[r] = (s[r] + ar_f) * (1 + ar_p / 100.0) * more.get(r, more.get("allResist", 1.0))
    # 6) 最终缩放
    s["health"] *= 10.0
    s["regen"] *= 1.5
    s["absorption"] *= 5.0
    s["dot"] = s["bleed"] + s["decay"] + s["burn"] + s["poison"]
    s["weight"] = _class_weight(cls)
    return s

def _hero_dps_to_monster(sh, mon_l, boss=False):
    """对单体每秒期望伤害：普攻（含护甲/闪避/暴击）+ DoT。"""
    atk = sh["attack"] + sh["elemDamage"]          # 元素并入（未按元素抗性拆，近似）
    crit = min(sh["critChance"], 5.0)              # 平均 1 + cc×(M−1)，cc 可 >1
    m_avg = 1 + crit * (1 + sh["critDamage"])      # critDamage 为 +x 分数（M=2+cd → 增量 cd）
    cut = _monster_cut(mon_l, sh["level"], min(sh["penetration"], 1.0))
    hit_avg = (atk * m_avg) * (1 - cut)
    # 怪闪避（敏捷 ×1 普怪/首领）
    ev = 1.875 * (100 + 12 * mon_l)
    miss = ev / (ev + 5 * sh["accuracy"]) if sh["accuracy"] > 0 else 0.273
    aps = 1 * (1 + sh["haste"])
    gap_f = 1.0 / _gap_factor(mon_l, sh["level"], boss)
    dps = hit_avg * (1 - miss) * aps * gap_f + sh["dot"] * gap_f
    return max(dps, 0.01)

def _monster_dps_to_hero(sh, mon_l, dmg, boss=False, stage_l=None):
    """怪对英雄每秒期望伤害：1 秒/击，走 dodge→减伤→格挡→吸收 全链。"""
    dmg = dmg * (1.0 if stage_l is None else _early_scale(stage_l))
    gap = max(0, mon_l - sh["level"])
    dmg *= _gap_factor(mon_l, sh["level"], boss)
    # 闪避
    d = sh["dodge"]
    dc = d / (d + 5 * (100 + 12 * mon_l)) if d > 0 else 0.0
    dc *= _dodge_keep(gap)
    landed = dmg * (1 - min(dc, 0.95))
    # 物理减伤（怪物 elemental 部分未拆，全按物理近似）
    red = 0.85 * sh["weight"] * sh["defense"] / (sh["defense"] + 50 + 6 * mon_l) if sh["defense"] > 0 else 0
    part = landed * (1 - red)
    # 格挡（物理）
    b = sh["block"]
    if b > 0:
        bc = b / (b + 5 * (100 + 12 * mon_l))
        part *= (1 - 0.5 * min(bc, 1.0))
    # 吸收
    a = sh["absorption"]
    if a > 0 and part > 0:
        part = part * (5 * part) / (a + 5 * part)
    return max(part, 0.01), min(dc, 0.95)

# 怪群/清关模型常量（文档未载，默认值 + 通关用时校准修正）
HORDES, HORDE_SIZE, WALK_S, KILL_GOAL = 6, 5, 2.0, 30

def _chest_rate(stage_l, kills_h):
    """普通宝箱/时（chests.md 公式，无掉落加成）。"""
    kps = kills_h / 3600.0
    if kps <= 0:
        return 0.0
    lv = stage_l
    # 时间地板（表内插值）
    fl_tbl = [(1, 216), (10, 405), (20, 405), (30, 607.5), (50, 652.5), (75, 1080), (90, 1260), (100, 1260)]
    floor = 216.0
    for (a, va), (b, vb) in zip(fl_tbl, fl_tbl[1:]):
        if a <= lv <= b:
            floor = va + (vb - va) * (lv - a) / (b - a)
            break
    r_hp = (math.exp(3.6554 + 0.67396 * math.log(max(lv, 1)) + 0.056393 * lv)
            / math.exp(3.6554 + 0.67396 * math.log(30) + 0.056393 * 30)) if lv > 30 else 1.0
    egf = 1 + 0.05 * (r_hp ** 0.48 - 1) if lv > 30 else 1.0
    guarantee = round(90 * (1 + 0.0018 * max(0, lv - 20) ** 2) / egf)
    kpc = max(guarantee, math.ceil(floor * kps))
    return kps * 3600.0 / kpc

def _kept_xp(hero_l, stage_l):
    """等级差经验保留（leveling.md）。"""
    gap = stage_l - hero_l
    if gap <= -3:      # 英雄高出
        over = hero_l - stage_l - 6
        if over <= 0:
            return 1.0
        return max(0.01, 1.0 / (1 + (over / 2.0) ** 1.585))
    if gap <= 3:
        return 1.0
    return max(0.01, 1.0 - 0.99 * (gap - 3) / 7.0)

def _catchup(levels, idx):
    """追赶 ×1.5：reference = 其他英雄中与最高差 ≤9 的最低者。"""
    ls = levels
    if len(ls) < 2:
        return 1.0
    hi = max(ls)
    others = [l for i, l in enumerate(ls) if i != idx]
    ref_cands = [l for l in others if hi - l <= 9]
    ref = min(ref_cands) if ref_cands else (min(others) if all(hi - l > 9 for l in others) else hi)
    return 1.5 if ls[idx] < ref - 3 else 1.0

def _stage_data():
    """摊平 [(mslug, zh, st)]。"""
    out = []
    for mslug, m in STAGES.items():
        zh = m.get("zh") or mslug
        for st in m.get("stages") or []:
            out.append((mslug, zh, st))
    return out

def _verdict(surv, boss_time, gap):
    """关卡评价近似：由守卫对首领的存活时间映射。"""
    if surv < boss_time:
        return "致命"
    if surv < boss_time * 2:
        return "危险"
    if surv < boss_time * 4:
        return "困难"
    if gap > 3:
        return "适中"
    return "简单"

def sim_table(heroes, calib=None, cur_act=None):
    """全关表：rows + 推荐位。calib = {factor, mon_l}（通关用时校准）。cur_act = 0 基章号。"""
    if not heroes or not STAGES:
        return None
    sheets = [hero_sheet(h) for h in heroes]
    levels = [s["level"] for s in sheets]
    max_lv = max(levels)
    # 守卫 = 第一个重甲英雄（boss 目标）
    g_idx = next((i for i, s in enumerate(sheets) if s["class"] in HEAVY), 0)
    guard = sheets[g_idx]
    calib_f = 1.0
    if calib:
        calib_f = float(calib.get("factor") or 1.0)
    rows = []
    for mslug, zh, st in _stage_data():
        L = int(st["level"])
        if L > max_lv + 6:
            continue
        # 队伍对普怪/首领的 DPS（按怪等级重算）
        dps_m = sum(_hero_dps_to_monster(s, L) for s in sheets)
        dps_b = sum(_hero_dps_to_monster(s, L, boss=True) for s in sheets)
        t_kill = max(st["hp"] * HORDE_SIZE / max(dps_m, 0.01), 0.02)
        t_clear = KILL_GOAL * t_kill + (HORDES - 1) * WALK_S
        boss_hp, boss_hit = st.get("boss_hp") or 0, st.get("boss_hit") or 0
        boss_time = 0.0
        surv = 99.0
        if boss_hp:
            boss_time = max(boss_hp / max(dps_b, 0.01), 0.1)
            gap_b = max(0, L - guard["level"])
            mps, _dc = _monster_dps_to_hero(guard, L, boss_hit, boss=True)
            if gap_b > 3:  # 怒气：60s 后 ×2，90s ×4 …
                t60 = 60.0
                if boss_time > t60:
                    mult, tt = 2.0, 60.0
                    while boss_time > tt + 30 and mult < 64:
                        tt += 30
                        mult *= 2
                    mps *= mult
            surv = guard["health"] / mps if mps > 0 else 99.0
            t_clear += boss_time
        f = calib_f
        if calib and calib.get("mon_l"):
            # 校准按怪强度距离加权（近=1，远衰减）
            w = math.exp(-abs(L - int(calib["mon_l"])) / 12.0)
            f = 1.0 + (calib_f - 1.0) * max(w, 0.15)
        t_clear *= f
        kills_h = KILL_GOAL / t_clear * 3600.0 if t_clear > 0 else 0.0
        gold_h = st["gold"] * kills_h * (1 + sum(s["goldFind"] for s in sheets))
        # 每英雄经验/时（保留比例 × 追赶）
        xp_each = [st["xp"] * kills_h * _kept_xp(lv, L) * _catchup(levels, i)
                   for i, lv in enumerate(levels)]
        chests_h = _chest_rate(L, kills_h)
        rows.append({"map": "%s %s" % (zh, st["stage"]), "slug": mslug, "stage": st["stage"],
                     "lv": L, "kills_h": kills_h, "gold_h": gold_h,
                     "xp_h": sum(xp_each), "xp_each": xp_each, "chests_h": chests_h,
                     "surv": surv, "boss_time": boss_time,
                     "verdict": _verdict(surv, boss_time, max(0, L - guard["level"])) if boss_hp else "—",
                     "t_clear": t_clear, "t_kill": t_kill})
    # 推荐位（wiki 口径）
    best_xp = best_gold = None
    pool = rows
    if cur_act is not None:
        keys = list(STAGES.keys())
        idx = max(0, min(int(cur_act), len(keys) - 1))
        allow = set(keys[:idx + 1])
        pool = [r for r in rows if r["slug"] in allow]
    hi_xp = max((r["xp_each"][i] / r["t_clear"] for r in pool for i in range(len(levels))), default=0)
    for r in sorted(pool, key=lambda r: -r["xp_h"]):
        if any(_kept_xp(lv, r["lv"]) >= 0.5 for lv in levels):
            best_xp = r
            break
    for r in sorted(pool, key=lambda r: -r["gold_h"]):
        if r["verdict"] not in ("致命", "危险"):
            best_gold = r
            break
    return {"rows": rows, "best_xp": best_xp, "best_gold": best_gold,
            "lv": max_lv, "guard": g_idx,
            "heroes": [(s["class"], s["level"]) for s in sheets]}

def offline_rows(heroes, cur_slug, cur_stage):
    """离线收益：当前关及以下，找无人倒下的最近关；无首领无宝箱。"""
    if not heroes or not STAGES:
        return None
    sheets = [hero_sheet(h) for h in heroes]
    guard = next((s for s in sheets if s["class"] in HEAVY), sheets[0])
    rank = {k: i for i, k in enumerate(STAGES.keys())}
    out = []
    for mslug, zh, st in _stage_data():
        if rank.get(mslug, 99) > rank.get(cur_slug, 99):
            continue
        L = int(st["level"])
        dps_m = sum(_hero_dps_to_monster(s, L) for s in sheets)
        t_kill = max(st["hp"] * HORDE_SIZE / max(dps_m, 0.01), 0.02)
        mps, _ = _monster_dps_to_hero(guard, L, st["hit"], stage_l=L)
        surv = guard["health"] / mps if mps > 0 else 0
        if surv < t_kill * 3:      # 一波都撑不过 = 倒下
            continue
        t_cycle = KILL_GOAL * t_kill + (HORDES - 1) * WALK_S
        kills_h = KILL_GOAL / t_cycle * 3600.0
        out.append({"map": "%s %s" % (zh, st["stage"]), "lv": L,
                    "gold_h": st["gold"] * kills_h * (1 + sum(s["goldFind"] for s in sheets)),
                    "xp_h": st["xp"] * kills_h * sum(_kept_xp(s["level"], L) * _catchup([s["level"] for s in sheets], i)
                                                     for i, s in enumerate(sheets)),
                    "surv": surv})
        if mslug == cur_slug and int(st["stage"].split("-")[1]) == cur_stage:
            break
    out.reverse()
    return out[:12]

def read_save_full():
    """读宿主/沙盒存档 → (heroes, cur_act, cur_stage)。"""
    import glob
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
    if not best:
        return None
    d = json.load(open(best[1], encoding="utf-8"))
    g = d.get("game")
    if isinstance(g, str):
        g = json.loads(g)
    g = g.get("game", g)
    heroes = [{"classId": h.get("classId"), "level": int(h.get("level", 0) or 0),
               "treeNodes": list(h.get("treeNodes") or []), "equipment": h.get("equipment") or {}}
              for h in (g.get("heroes") or [])]
    return heroes, int(g.get("act", 0) or 0), int(g.get("stage", 0) or 0)

def time_to_level(xp_h, level):
    level = int(level)
    if xp_h <= 0 or level < 1 or level > len(XP_TABLE):
        return None
    need = XP_TABLE[level - 1]
    return need / xp_h * 3600.0 if need > 0 else None
