# 第三方依赖与许可证登记

> 用途：登记本项目复用的一切第三方组件（含代码、脚手架、模型 API、开源 harness 基座等）的
> **出处与许可证**。依据：复用部分必须标注出处与许可证，**不得作为自身创新点申报**。

| 组件 / 来源 | 版本 / 链接 | 许可证 | 用途 | 是否与创新点相关 |
|---|---|---|---|---|
| Monaco Editor（Microsoft） | `0.57.0` · https://github.com/microsoft/monaco-editor | **MIT** | 本地网页代码编辑器，许可证随包保存于 `web/vendor/monaco/LICENSE` | 否 |
| FastAPI | https://github.com/fastapi/fastapi | MIT | Web API 服务框架 | 否 |
| Uvicorn | https://github.com/encode/uvicorn | BSD-3-Clause | ASGI 服务进程 | 否 |
| python-dotenv | https://github.com/theskumar/python-dotenv | BSD-3-Clause | 读取本地环境配置 | 否 |
| OpenAI Python SDK | https://github.com/openai/openai-python | Apache-2.0 | 调用兼容模型接口 | 否 |
| pytest | https://github.com/pytest-dev/pytest | MIT | 自动化测试 | 否 |

依赖版本范围见 `requirements.txt`，这里不是锁定环境的完整依赖清单。安装时产生的传递依赖按各自许可证处理。SDK的许可不代表模型、模型输出或服务可按同一许可使用；模型服务由部署者自行配置。

> **说明（2026-10-17）**：前端编辑器由 CodeMirror（CDN 版，已移除）更换为**本地化 Monaco**。
> 只启用 **python 语法着色**（`basic-languages`），**未接入**任何语言服务/自动补全
> —— 补全与签名提示等同于把 API 用法直接喂给学生，与"绝不给答案"的核心主张冲突。
> 许可证原文随包存放：`web/vendor/monaco/LICENSE`。

## 说明

- 本项目**自研**范围：教学法 Harness 约束层、三个教学法组件、组件开关、判题运行器、日志埋点。
- 本表登记的是**基座/基础设施**类复用，与上述自研部分严格区分。
- 引入任何新依赖时，请同步在本表补一行。
