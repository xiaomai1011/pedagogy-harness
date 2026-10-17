"""测试专用配置：单测一律用**临时库**，绝不往真实的 `pedagogy.sqlite` 写数据。

为什么需要：`app/db.py` 在 import 时就从环境变量 `PH_DB` 读路径。测试若共用一个
持久库，跨次运行留下的历史行会污染"连败次数 / 已解锁等级 / 提交序号"这类**有状态**断言
（本轮就撞上过：`hint_level` 断言被上一轮残留的行顶成了 2）。

pytest 会**先导入 conftest.py**，再导入测试模块，所以这里设的环境变量一定能生效。
"""

import os
import tempfile
from pathlib import Path

_TEST_DB = Path(tempfile.gettempdir()) / "pedagogy_harness_test.sqlite"
_TEST_DB.unlink(missing_ok=True)          # 每次跑测试都从空库开始
os.environ["PH_DB"] = str(_TEST_DB)
