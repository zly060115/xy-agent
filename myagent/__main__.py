"""
兼容入口：python -m myagent
实际逻辑在项目根目录的 main.py
"""

import sys
from pathlib import Path

# 把项目根目录加入 path，这样才能 import main
ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from main import main

if __name__ == "__main__":
    main()
