# NewQAHelper-Vka (OPENCV-ManualVersion)

Quest Away 桌面助手（RE 版源码）。OpenCV 模板匹配 + 手动版本定位，理论收益计算基于 wiki 镜像数据。

## 结构

- `qa_re.pyw` — 主程序（挂机/收益分析/市场监控）
- `recap.py` / `rehand.py` / `relook.py` / `redata.py` — 功能模块
- `_extract_theory.py` — 从 wiki 镜像抽取关卡/职业/天赋数据（`_stages.json` / `_class_stats.json` / `_tree.json`）
- `_xp_table.json` / `_names.json` / `_market_names.json` — 静态数据
- `tpl/` — OpenCV 模板图
- `QA助手RE.spec` — PyInstaller 打包配置（含数据文件）
