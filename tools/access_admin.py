"""访问码管理（负责人本地使用）：生成签名密钥 / 签发单个参与者的阶段访问码。

用法（在仓库根目录、用项目 venv 的 python）：

    # 1) 生成签名密钥（只做一次；不要提交、不要贴到聊天/截图里）
    python tools/access_admin.py new-secret --out C:\\path\\outside\\repo\\ph_access_secret.txt

    # 2) 为某个参与者签发某阶段的访问码（正式码由负责人签发，DS 只提供工具）
    set PH_ACCESS_SECRET_FILE=C:\\path\\outside\\repo\\ph_access_secret.txt
    python tools/access_admin.py issue --pid S001 --batch B2026A --phase practice --hours 72

设计说明（务必知道）：
- HMAC 是**无状态**的：签发后服务端不保存任何记录，所以**不能即时撤销**。
  到期前旧码一直有效。阶段结束时让旧权限失效的最小办法是**轮换签名密钥**
  （换新文件 → 重启服务），这会让**所有**旧码同时失效。
- 因此每个阶段单独签发、有效期只覆盖该阶段窗口+少量缓冲，不要一次发全阶段。
- 访问码不要放进 URL、不要写日志；只在负责人监督下当面发放。
"""
from __future__ import annotations

import argparse
import os
import pathlib
import secrets
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def cmd_new_secret(args) -> int:
    path = pathlib.Path(args.out)
    if path.exists() and not args.force:
        print(f"refuse: {path} 已存在（加 --force 覆盖）")
        return 2
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(secrets.token_hex(32), encoding="ascii")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    print(f"written {path} (64 hex chars)；请把它放在仓库之外，并只在服务端以 PH_ACCESS_SECRET_FILE 指向")
    return 0


def cmd_issue(args) -> int:
    from app import access

    phases = tuple(p.strip() for p in args.phase.split(",") if p.strip())
    code = access.issue_code(args.pid, batch=args.batch, phases=phases,
                             ttl_seconds=int(args.hours * 3600))
    print(code)
    print(f"# pid={args.pid} batch={args.batch} phases={','.join(phases)} ttl={args.hours}h",
          file=sys.stderr)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("new-secret", help="生成 32 字节随机签名密钥")
    p1.add_argument("--out", required=True)
    p1.add_argument("--force", action="store_true")
    p1.set_defaults(func=cmd_new_secret)

    p2 = sub.add_parser("issue", help="签发一个访问码")
    p2.add_argument("--pid", required=True)
    p2.add_argument("--batch", required=True)
    p2.add_argument("--phase", required=True, help="逗号分隔：pretest,practice,posttest")
    p2.add_argument("--hours", type=float, default=24.0)
    p2.set_defaults(func=cmd_issue)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
