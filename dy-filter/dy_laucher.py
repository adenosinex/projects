# -*- coding: utf-8 -*-
"""
dy_laucher.py — 薄启动壳（兼容原有双击/命令行习惯）
=========================================================================
真正的实现已合并进 utils.py，此处仅做转发，避免代码重复。

等价命令:
    python dy_laucher.py      ==  python utils.py download
=========================================================================
"""

from utils import download_and_launch

if __name__ == "__main__":
    download_and_launch()
