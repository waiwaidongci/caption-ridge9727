"""单侧精确二项符号检验（exact one-sided sign test）。

用于方案试跑的逐单绝对误差比较：只统计新、旧方案误差不相等的工单，
平局不计。原假设为两方案优劣等概率（p=0.5），备择假设为新方案更优
（新方案绝对误差更小的概率超过一半）。检验统计量是新方案更优的单数，
p 值取 Binomial(n, 0.5) 上尾概率，即 P(X >= wins)。
"""

from math import comb

ALPHA = 0.025


def sign_pvalue(wins, losses):
    """返回单侧精确符号检验 p 值。wins=新方案更优单数，losses=旧方案更优单数。"""
    n = wins + losses
    if n == 0:
        return 1.0
    # 上尾：P(X >= wins)，X ~ Binomial(n, 0.5)
    return sum(comb(n, k) for k in range(wins, n + 1)) / (2 ** n)


def critical_wins(n, alpha=ALPHA):
    """给定非平局样本量 n，返回达到显著性所需的最小新方案更优单数。"""
    for wins in range(n + 1):
        if sign_pvalue(wins, n - wins) <= alpha:
            return wins
    return None


def evaluate(paired):
    """paired 为 (job_id, old_abs_error, new_abs_error) 序列，返回逐单判定与汇总。"""
    jobs, wins, losses, ties = [], 0, 0, 0
    for job_id, old_error, new_error in paired:
        if abs(new_error - old_error) < 1e-12:
            verdict = "平局"
            ties += 1
        elif new_error < old_error:
            verdict = "新方案更优"
            wins += 1
        else:
            verdict = "旧方案更优"
            losses += 1
        jobs.append({"job_id": job_id, "old_abs_error": round(old_error, 4),
                     "new_abs_error": round(new_error, 4), "verdict": verdict})
    n = wins + losses
    p_value = sign_pvalue(wins, losses)
    return {
        "jobs": jobs,
        "wins": wins,
        "losses": losses,
        "ties": ties,
        "compared": n,
        "p_value": round(p_value, 6),
        "alpha": ALPHA,
        "critical_wins": critical_wins(n) if n else None,
        "passed": n > 0 and p_value <= ALPHA,
    }
