"""实验访问码（最小实现，2026-10-28）。

目标（评阅口径）：**参与者身份由服务端从凭证确定，不信任请求体里自填的 ID**。
- 凭证 = `v1.<payload_b64url>.<sig_b64url>`，签名 HMAC-SHA256；
- payload 绑定 `pid`（参与者）、`batch`（实验批次）、`phases`（允许的题目阶段）、`exp`（过期时间）；
- 密钥只从 `PH_ACCESS_SECRET` 或 `PH_ACCESS_SECRET_FILE` 指向的文件读取，**不进仓库、不下发前端**；
- 访问码只走请求头 `X-Access-Code`，**不放 URL 查询参数**（避免进访问日志/浏览器历史）；
- 缺失、过期、签名不符、身份不匹配一律拒绝，且在进入判题/模型之前拒绝。

限制（必须写进实验记录）：HMAC 只能防伪造，**不能防窃取和转发**；远程明文 HTTP 下
拿到访问码的人可以冒用。因此当前只支持"负责人监督下的本机/受控核验"，
校园网明文访问不得宣称为安全方案。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass

HEADER_NAME = "X-Access-Code"
TOKEN_VERSION = "v1"
DEFAULT_TTL_SECONDS = 12 * 3600


class AccessError(Exception):
    """访问码相关错误（受控码用于日志/响应，不含凭证原文）。"""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


class AccessNotConfigured(AccessError):
    def __init__(self):
        super().__init__("access_not_configured", "实验访问码未配置")


@dataclass(frozen=True)
class Claims:
    participant_id: str
    batch: str
    phases: tuple[str, ...]
    expires_at: int

    def allows_phase(self, phase: str) -> bool:
        return phase in self.phases


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _secret() -> bytes:
    env = os.environ.get("PH_ACCESS_SECRET", "").strip()
    if env:
        return env.encode("utf-8")
    path = os.environ.get("PH_ACCESS_SECRET_FILE", "").strip()
    if path and os.path.exists(path):
        with open(path, "rb") as fh:
            return fh.read().strip()
    raise AccessNotConfigured()


def access_required() -> bool:
    """是否强制访问码。正式采集必须开启；本地演示/单测默认关闭。"""
    return os.environ.get("PH_REQUIRE_ACCESS_CODE", "0").strip() == "1"


def issue_code(participant_id: str, *, batch: str, phases: tuple[str, ...] = ("practice",),
               ttl_seconds: int = DEFAULT_TTL_SECONDS, now: int | None = None) -> str:
    if not participant_id:
        raise AccessError("bad_participant", "参与者 ID 不能为空")
    issued = int(now if now is not None else time.time())
    payload = {"v": 1, "pid": participant_id, "batch": batch,
               "phases": list(phases), "iat": issued, "exp": issued + int(ttl_seconds)}
    body = _b64e(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    sig = hmac.new(_secret(), f"{TOKEN_VERSION}.{body}".encode("ascii"), hashlib.sha256).digest()
    return f"{TOKEN_VERSION}.{body}.{_b64e(sig)}"


def verify_code(code: str | None, *, now: int | None = None) -> Claims:
    if not code:
        raise AccessError("missing_code", "缺少实验访问码")
    parts = code.split(".")
    if len(parts) != 3 or parts[0] != TOKEN_VERSION:
        raise AccessError("malformed_code", "访问码格式不正确")
    body, sig_text = parts[1], parts[2]
    try:
        sig = _b64d(sig_text)
    except Exception:
        raise AccessError("malformed_code", "访问码签名格式不正确")
    expect = hmac.new(_secret(), f"{TOKEN_VERSION}.{body}".encode("ascii"),
                      hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expect):
        raise AccessError("bad_signature", "访问码签名校验失败")
    try:
        payload = json.loads(_b64d(body).decode("utf-8"))
    except Exception:
        raise AccessError("malformed_code", "访问码内容无法解析")
    exp = int(payload.get("exp") or 0)
    if exp and int(now if now is not None else time.time()) >= exp:
        raise AccessError("expired_code", "访问码已过期")
    pid = str(payload.get("pid") or "")
    if not pid:
        raise AccessError("bad_payload", "访问码缺少参与者标识")
    return Claims(participant_id=pid, batch=str(payload.get("batch") or ""),
                  phases=tuple(payload.get("phases") or ()), expires_at=exp)


def resolve_participant(body_participant_id: str, claims: Claims | None) -> str:
    """有凭证就用凭证里的身份；请求体只允许与之一致（不一致直接拒绝）。"""
    if claims is None:
        return body_participant_id
    if body_participant_id and body_participant_id != claims.participant_id:
        raise AccessError("identity_mismatch", "请求体参与者与访问码不一致")
    return claims.participant_id
