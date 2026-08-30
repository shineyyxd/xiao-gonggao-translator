# -*- coding: utf-8 -*-
"""pytest 公共配置：把 pipeline/ 目录加入 sys.path，使测试可按模块名直接导入。"""
import sys
from pathlib import Path

PIPELINE_DIR = Path(__file__).resolve().parent.parent
if str(PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(PIPELINE_DIR))
