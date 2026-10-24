"""仅为独立包测试提供唯一命名的 Fake 模块，避免宿主 tests 包冲突。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
