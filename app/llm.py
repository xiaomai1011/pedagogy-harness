"""统一大模型调用入口。

约定：全项目所有组件**只能**通过本函数调用大模型。
好处：① 实验组与对照组的模型行为完全一致，两组只差 Harness 壳；
      ② 便于统一控制参数（温度、长度），保证实验可比性。

配置（写在本机 .env 里，模板见 .env.example；**.env 不入库**）：
    PH_LLM_BASE_URL   中转站 / OpenAI 兼容接口地址（通常以 /v1 结尾）
    PH_LLM_API_KEY    密钥
    PH_LLM_MODEL      模型名
    PH_LLM_REASONING_EFFORT
                      可选。思考强度；**留空＝不发送该参数**（代码默认即留空）。
                      **未配置时兼容原调用方式；显式启用该参数时，必须使用
                      经验证支持它的 SDK 与服务端**——见下方 DEFAULT_REASONING_EFFORT 注释。

⚠️ 严禁把真实 Key 写进代码、聊天记录或任何提交。
"""

import os
import threading
import time

from dotenv import load_dotenv

load_dotenv()  # 读取本机 .env（若存在）；不会覆盖已存在的环境变量

#: 线程局部计时：`llm()` 每次调用后把耗时写到这里（见 `last_elapsed_ms`）。
_TLS = threading.local()

REQUIRED = ("PH_LLM_BASE_URL", "PH_LLM_API_KEY", "PH_LLM_MODEL")

# 实验可比性考虑：温度调低，减少同一输入的随机波动
DEFAULT_TEMPERATURE = 0.2
# 3000 而非 800：当前中转站的模型是**推理型**，先出 reasoning_content 再出正文，
# 上限太低会把 token 耗在思考上、正文被截成空串（实测 step-3.7-flash 在 800 下
# JSON 可解析率仅 33%、content 为空；提到 3000 后为 100%）。见 tools/model_probe.py。
DEFAULT_MAX_TOKENS = 3000
# 思考强度：**默认不发送** `reasoning_effort`，即使用服务端的默认行为。
#
# 为什么默认不发：① 保留旧 SDK 兼容性——该参数在 openai SDK **1.60.0** 才进入
# `chat.completions.create()` 签名（1.30.0–1.55.0 均无此参数、也没有 **kwargs 兜底，
# 传了会 TypeError）；requirements.txt 的 `openai>=1.30` 下限因此仍然有效。
# ② 「不发送」只是**采用服务端默认**，不代表「开启思考」，不该由客户端替服务端假定。
#
# ⚠️ **兼容性的边界（部署说明，勿省略）**：
# 「默认不发送」解决的是**旧配置下的兼容**，**不是「启用参数后也兼容」**。
# 若另一台机器装的是依赖范围允许的旧版 SDK（如 1.30–1.55），**即使只设
# `PH_LLM_REASONING_EFFORT=none` 也会因不支持该参数而报错**。
# ⇒ 口径：**未配置时兼容原调用方式；显式启用该参数时，必须使用经验证支持它的
# SDK 与服务端组合。**
#
# ⚠️ 另需注意：「签名里有该参数」**不等于**该版本正式支持 `"none"` 这个取值，
# 也**不等于**中转站会执行它。本项目目前的在线观察只支持
# **「当前安装版本（openai 3.31.0）＋当前中转站」**这一组合，
# **不能外推到所有 `>=1.60` 的版本或别的中转站**；换机部署时须一并记录版本并复核。
#
# 已观察到的事实（**限定范围**）：在**当前中转站、当前模型别名**下，对本批**分类请求**
# 发送 `reasoning_effort="none"` 时延迟较低（均值 1133ms vs 不发送 1821ms，n=12），
# 且返回的 reasoning 字段为空。**不能**推广成「所有提示任务都稳定快约 38%」；
# reasoning 字段为空**不等于**证明服务内部完全没有推理；更**不等于**提示分级更合理。
#
# 两批小样本的 L4 首次规则通过率方向**不一致**（n=10 为 10/10 vs 7/10，n=3 为 33% vs 100%），
# 故**两批数字都保留**，且**不作为教学质量优劣的证据**——首次通过防泄规则 ≠ 提示正确、
# 分级合理或对学生有帮助。这些维度须待真实判题接通后用固定样例审核。
#
# 需要显式启用时，由**本机配置**选择：`PH_LLM_REASONING_EFFORT=none`。
# 设为**空字符串**同样表示不发送（用于 A/B 对比，不需要改代码）。
DEFAULT_REASONING_EFFORT = ""


def is_configured() -> bool:
    """三项配置是否齐全。"""
    return all(os.environ.get(k) for k in REQUIRED)


# **可预期**的模型故障：超时／连接／限流／服务端错误 —— 允许调用方**受控降级**（契约 §6.3 硬规则 4）。
# 按异常**类名**判定，避免在未安装 openai 时强行导入；同时不把程序缺陷误判成"可预期"。
_EXPECTED_FAILURE_NAMES = frozenset((
    "APITimeoutError", "APIConnectionError", "RateLimitError", "InternalServerError",
    "Timeout", "TimeoutError", "ConnectError", "ConnectionError", "ReadTimeout",
    "RemoteProtocolError", "APIError",
))


def is_expected_failure(exc: BaseException) -> bool:
    """判断模型故障是否**可预期**。

    可预期 → 调用方可以受控降级（返回兜底提示并如实记来源）；
    **不可预期**（代码缺陷、解析错误等）→ 必须**暴露**，不得用兜底吞掉。
    """
    return type(exc).__name__ in _EXPECTED_FAILURE_NAMES


def _reasoning_effort() -> str | None:
    """本次调用要发送的 `reasoning_effort`；`None` 表示**不发送该参数**。

    默认取 `DEFAULT_REASONING_EFFORT`（当前为**空串 ⇒ 不发送**，见上方说明）。
    环境变量 `PH_LLM_REASONING_EFFORT` 可覆盖；显式设为空串即不发送，
    设为 `none`／`low` 等则发送——用于「有思考 / 无思考」的 A/B 对比。
    """
    raw = os.environ.get("PH_LLM_REASONING_EFFORT")
    value = DEFAULT_REASONING_EFFORT if raw is None else raw.strip()
    return value or None


def _sanitize_proxy_env() -> None:
    """剔除会毒化 HTTP 客户端的 `NO_PROXY` 条目。

    背景（2026-10-28 实测）：本机环境里 `NO_PROXY=localhost,127.0.0.1,::1,[::1]`，
    其中的方括号 IPv6 写法 `[::1]` 会让 HTTP 客户端在**建连接池时**就抛
    `httpx2.InvalidURL: Invalid port: ':1]'`——表现为"大模型一调用就崩"，
    与模型服务本身无关。这里只去掉带方括号的条目，代理本身照常生效。
    """

    for key in ("NO_PROXY", "no_proxy"):
        raw = os.environ.get(key)
        if not raw:
            continue
        parts = [item.strip() for item in raw.split(",") if item.strip()]
        keep = [item for item in parts if "[" not in item and "]" not in item]
        if keep != parts:
            os.environ[key] = ",".join(keep)


def llm(system: str, user: str, *, temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int = DEFAULT_MAX_TOKENS) -> str:
    """按 system/user 调用大模型，返回纯文本。

    未配置时返回桩文本（便于无 Key 环境下跑通链路），**不抛异常**。
    配置齐全但调用失败时，抛异常——让问题暴露，不要静默返回假内容。

    **计时**：每次调用把耗时记进线程局部变量，调用方用 `last_elapsed_ms()` 取。
    这样分类兜底、提示生成、重试等**所有**模型路径都能被统计，而不是只包住提示那一次。
    """
    started = time.perf_counter()
    try:
        return _llm_impl(system, user, temperature=temperature, max_tokens=max_tokens)
    finally:
        spent = int((time.perf_counter() - started) * 1000)
        _TLS.elapsed_ms = spent
        _TLS.total_ms = int(getattr(_TLS, "total_ms", 0) or 0) + spent


def reset_timing() -> None:
    """请求开始时清零累计计时（一次请求可能调用模型多次：分类兜底、提示、重试）。"""
    _TLS.elapsed_ms = 0
    _TLS.total_ms = 0


def last_elapsed_ms() -> int:
    """本线程**最近一次** `llm()` 调用的耗时（毫秒）；没调用过返回 0。"""
    return int(getattr(_TLS, "elapsed_ms", 0) or 0)


def total_elapsed_ms() -> int:
    """自上次 `reset_timing()` 起，本线程**所有**模型调用耗时之和（毫秒）。"""
    return int(getattr(_TLS, "total_ms", 0) or 0)


def _llm_impl(system: str, user: str, *, temperature: float,
              max_tokens: int) -> str:
    if not is_configured():
        return ("[桩·大模型] 未配置 PH_LLM_BASE_URL / PH_LLM_API_KEY / PH_LLM_MODEL，"
                "此处为占位响应。")

    _sanitize_proxy_env()

    from openai import OpenAI  # 延迟导入：未配置时不强制安装 openai

    client = OpenAI(
        api_key=os.environ["PH_LLM_API_KEY"],
        base_url=os.environ["PH_LLM_BASE_URL"],
        timeout=60,
    )
    kwargs: dict = {}
    effort = _reasoning_effort()
    if effort is not None:
        kwargs["reasoning_effort"] = effort
    resp = client.chat.completions.create(
        model=os.environ["PH_LLM_MODEL"],
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=temperature,
        max_tokens=max_tokens,
        **kwargs,
    )
    return (resp.choices[0].message.content or "").strip()
