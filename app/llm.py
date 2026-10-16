"""统一大模型调用入口。

约定：全项目所有组件**只能**通过本函数调用大模型。
好处：① 实验组/对照组的模型行为完全一致，只差 Harness 壳；② 便于统一记录耗时/token。

⚠️ API Key 只从环境变量读取，**严禁写进代码或提交到仓库**（放在 .env，.env 不入库）。
"""

import os

API_KEY = os.environ.get("PH_LLM_API_KEY", "")
BASE_URL = os.environ.get("PH_LLM_BASE_URL", "")
MODEL = os.environ.get("PH_LLM_MODEL", "")


def llm(system: str, user: str) -> str:
    """按 system/user 调用大模型，返回纯文本。

    TODO(岗位 1, 10/18—10/20)：接入真实模型 API。
    当前为桩：返回固定文本，便于在无 Key 环境下跑通纵切链路。
    """
    if not API_KEY:
        return "[桩·大模型] 未配置 PH_LLM_API_KEY，此处为占位响应。"
    # TODO: 用 openai SDK 或其他兼容接口真实调用
    raise NotImplementedError("真实大模型调用尚未接入（Phase 1 骨架）")
