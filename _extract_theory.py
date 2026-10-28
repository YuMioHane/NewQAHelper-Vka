# -*- coding: utf-8 -*-
"""抽取理论收益所需数据 → _stages.json / _class_stats.json / _tree.json。

数据源（本机 wiki 镜像 + 天赋树库，仅构建期读取，运行时随 exe 打包）：
  - questaway-wiki/markdown/maps/<map>.md      → 每关怪物 HP/每击/BOSS/金币/经验
  - questaway-wiki/markdown/classes/<cls>.md   → 职业基础属性与成长
  - .dsh/skills/questaway-wiki-db/data/passive-tree.json → 天赋效果与基石
wiki 大版本更新后重跑一次本脚本即可刷新三份数据。
"""
import json
import os
import re

WIKI = r"C:\Users\Vka\.dsh\profiles\desktop\questaway-wiki"
MD = os.path.join(WIKI, "markdown")
TREE_SRC = r"C:\Users\Vka\.dsh\profiles\desktop\.dsh\skills\questaway-wiki-db\data\passive-tree.json"
OUT = os.path.dirname(os.path.abspath(__file__))

MAPS = ["isles", "swamp", "forest", "desert", "canyon", "mountains", "glaciers", "volcano",
        "floating-isles", "deadlands"]
MAP_ZH = {"isles": "群岛", "swamp": "沼泽", "forest": "森林", "desert": "沙漠", "canyon": "峡谷",
          "mountains": "山地", "glaciers": "冰川", "volcano": "火山", "floating-isles": "浮空岛",
          "deadlands": "死寂之地"}
CLASSES = ["warrior", "mage", "healer", "archer", "assassin", "engineer", "barbarian",
           "monk", "witch", "paladin"]


def read(p):
    return open(p, encoding="utf-8", errors="replace").read()


def num(s):
    m = re.search(r"[\d,]+(?:\.\d+)?", str(s))
    return float(m.group(0).replace(",", "")) if m else 0.0


def table_rows(text):
    for line in text.split("\n"):
        if line.strip().startswith("|"):
            yield [c.strip() for c in line.strip().strip("|").split("|")]


def extract_stages():
    out = {}
    for m in MAPS:
        p = os.path.join(MD, "maps", m + ".md")
        if not os.path.exists(p):
            continue
        t = read(p)
        i = t.find("## Stages")
        if i < 0:
            continue
        stages = []
        seen = {}   # 同一关出现 3 次（Veil/Breach/Abyss 三难度），按表序 0/1/2
        for cells in table_rows(t[i:]):
            if not cells or all(set(c) <= set("- :") for c in cells):
                continue
            if cells[0] == "Stage" and len(cells) > 1 and cells[1] == "Level":
                continue
            if len(cells) >= 9 and re.match(r"^\d+-\d+$", cells[0].replace("*", "").strip()):
                name = cells[0].replace("*", "").strip()
                stages.append({
                    "stage": name,
                    "level": num(cells[1]), "hp": num(cells[2]), "hit": num(cells[3]),
                    "boss_hp": num(cells[4]), "boss_hit": num(cells[5]),
                    "gold": num(cells[6]), "xp": num(cells[7]), "ilvl": num(cells[8]),
                    "diff": seen.get(name, 0),
                })
                seen[name] = seen.get(name, 0) + 1
        out[m] = {"zh": MAP_ZH[m], "stages": stages}
    return out


def extract_class_stats():
    out = {}
    for cls in CLASSES:
        p = os.path.join(MD, "classes", cls + ".md")
        if not os.path.exists(p):
            continue
        stats = {}
        for cells in table_rows(read(p)):
            if len(cells) >= 4 and cells[0] in ("Defense", "Health", "Attack"):
                stats[cells[0]] = {"base": num(cells[1]), "per_level": num(cells[2])}
        out[cls] = stats
    return out


def extract_tree():
    t = json.load(open(TREE_SRC, encoding="utf-8"))
    return {"effects": t.get("effects") or {}, "keystones": t.get("keystones") or {}}


def main():
    st = extract_stages()
    n_stage = sum(len(v["stages"]) for v in st.values())
    cs = extract_class_stats()
    tr = extract_tree()
    json.dump(st, open(os.path.join(OUT, "_stages.json"), "w", encoding="utf-8"), ensure_ascii=False)
    json.dump(cs, open(os.path.join(OUT, "_class_stats.json"), "w", encoding="utf-8"), ensure_ascii=False)
    json.dump(tr, open(os.path.join(OUT, "_tree.json"), "w", encoding="utf-8"), ensure_ascii=False)
    print("maps=%d stages=%d classes=%d tree_effects=%d keystones=%d" % (
        len(st), n_stage, len(cs), len(tr["effects"]), len(tr["keystones"])))


if __name__ == "__main__":
    main()
