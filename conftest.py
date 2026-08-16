"""pytest 根配置：确保项目根目录在 sys.path，便于 ``import hexbroker``。"""

import pathlib
import sys

ROOT = pathlib.Path(__file__).parent.resolve()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
