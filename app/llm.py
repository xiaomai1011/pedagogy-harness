"""统一大模型调用入口。

约定：全项目所有组件**只能**通过本函数调用大模型。
好处：① 实验组与对照组的模型行为完全一致，两组只差 Harness 壳；
      ② 便于统一控制参数（温度、长度），保证实验可比性。

配置（写在本机 .env 里，模板见 .env.example；**.env 不入库**）：
    PH_LLM_BASE_URL   中转站 / OpenAI 兼容接口地址（通常以 /v1 结尾）
    PH_LLM_API_KEY    密钥
    PH_LLM_MODEL      模型名

⚠️ 严禁把真实 Key 写进代码、聊天记录或任何提交。
"""

import os

from dotenv import load_dotenv

load_dotenv()  # 读取本机 .env（若存在）；不会覆盖已存在的环境变量

REQUIRED = ("PH_LLM_BASE_URL", "PH_LLM_API_KEY", "PH_LLM_MODEL")

# 实验可比性考虑：温度调低，减少同一输入的随机波动
DEFAULT_TEMPERATURE = 0.2
# 3000 而非 800：当前中转站的模型是**推理型**，先出 reasoning_content 再出正文，
# 上限太低会把 token 耗在思考上、正文被截成空串（实测 step-3.7-flash 在 800 下
# JSON 可解析率仅 33%、content 为空；提到 3000 后为 100%）。见 tools/model_probe.py。
DEFAULT_MAX_TOKENS = 3000


def is_configured() -> bool:
    """三项配置是否齐全。"""
    return all(os.environ.get(k) for k in REQUIRED)


def llm(system: str, user: str, *, temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int = DEFAULT_MAX_TOKENS) -> str:
    """按 system/user 调用大模型，返回纯文本。

    未配置时返回桩文本（便于无 Key 环境下跑通链路），**不抛异常**。
    配置齐全但调用失败时，抛异常——让问题暴露，不要静默返回假内容。
    """
    if not is_configured():
        return ("[桩·大模型] 未配置 PH_LLM_BASE_URL / PH_LLM_API_KEY / PH_LLM_MODEL，"
                "此处为占位响应。")

    from openai import OpenAI  # 延迟导入：未配置时不强制安装 openai

    client = OpenAI(
        api_key=os.environ["PH_LLM_API_KEY"],
        base_url=os.environ["PH_LLM_BASE_URL"],
        timeout=60,
    )
    resp = client.chat.completions.create(
        model=os.environ["PH_LLM_MODEL"],
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=temperature,
        max_tokens=max_tokens,
    )
    return (resp.choices[0].message.content or "").strip()
