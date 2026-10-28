# -*- coding: utf-8 -*-
"""存档层：直读 QuestAway 存档（不依赖调试端口，官方封端口也不影响）。

存档路径（所有用户通用，不要写死用户名）：
    %APPDATA%\\com.artxgames.questaway\\save.json

RE 版的「大脑数据源」：英雄等级/背包/资源/buff 全在这，
换装评分、选图收益、炼药判定都从这里算，只有「执行」走点击。
"""
import json
import os
import time

SAVE_PATH = os.path.join(os.environ["APPDATA"],
                         "com.artxgames.questaway", "save.json")


def load(max_age=0.0):
    """读存档 dict。max_age 秒内读过且文件没变就返回缓存。

    文件不存在/还没生成（新玩家没过教学）返回 None。
    """
    if not os.path.exists(SAVE_PATH):
        return None
    m = os.path.getmtime(SAVE_PATH)
    if max_age and getattr(load, "_t", 0) == m:
        return load._cache
    try:
        with open(SAVE_PATH, encoding="utf-8") as f:
            g = json.load(f)
    except Exception:
        # 游戏正在写盘的瞬间可能读到半个文件，稍后重试自然恢复
        return getattr(load, "_cache", None)
    load._cache, load._t = g, m
    return g


def age():
    """存档多少秒前更新过（判断游戏是不是活着在存盘）。"""
    if not os.path.exists(SAVE_PATH):
        return -1
    return time.time() - os.path.getmtime(SAVE_PATH)
