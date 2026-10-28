"""核对提示阶梯在五个场景下的行为（实现级，不调用模型）。

输出一张表：场景 / 输入状态 / 展示等级 / 解锁上限，并与申报书原文对照标注。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.harness import hint_ladder  # noqa: E402


def step(name, unlocked, consecutive, requested):
    display, unlocked_after = hint_ladder.next_state(
        prev_unlocked=unlocked, consecutive_fail=consecutive, requested=requested
    )
    print(f"{name:<28} in(unlocked={unlocked},fails={consecutive},req={int(requested)}) "
          f"-> display=L{display} unlocked=L{unlocked_after}")
    return display, unlocked_after


print("=== 1. 首次主动请求（无失败） ===")
step("首次主动请求", 0, 0, True)

print("=== 2. 无新增失败时再次请求 ===")
step("再次请求（仍无失败）", 1, 0, True)

print("=== 3. 每累计两次连续失败 ===")
for fails in (1, 2, 3, 4, 6, 8):
    step(f"连续失败 {fails} 次后请求", 0, fails, True)
for fails in (2, 4, 6, 8):
    d = hint_ladder.next_unlock(prev_unlocked=0, consecutive_fail=fails)
    print(f"   next_unlock(prev=0, fails={fails}) -> L{d}（期望 L{min(fails // 2, 4)}）")

print("=== 4. 成功提交后（连续失败清零、解锁不回退） ===")
step("成功后请求（unlocked=2,fails=0）", 2, 0, True)
print("   next_unlock(prev=2, fails=0) ->",
      hint_ladder.next_unlock(prev_unlocked=2, consecutive_fail=0))

print("=== 5. 切题后（新题目作用域） ===")
step("切题后首次请求", 0, 0, True)

print("=== 不请求时（纯学习） ===")
step("未请求", 2, 4, False)
print("LEVEL_GUIDE keys:", sorted(hint_ladder.LEVEL_GUIDE))
for lv in sorted(hint_ladder.LEVEL_GUIDE):
    print(f"  L{lv}: {hint_ladder.LEVEL_GUIDE[lv][:40]}")
