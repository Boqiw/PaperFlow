"""领域词表：一张扁平的关键词清单。

它只做两件事：

1. 从你写的目标和笔记里认出「这一周该搜什么」（:func:`pick_queries`）；
2. 把论文标题软性归类，方便你一眼看出这周有没有偏（:func:`tag_titles`）。

刻意保持扁平：**不是图谱，没有依赖边，没有界面，不会出现在任何输出里**。
它存在的唯一理由是让「全局优化」可计算——没有它，冷启动就不知道该搜什么，
AI 也只能凭印象说话。
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Dict, List, Sequence, Tuple

#: ``(英文检索词, 中文标签)``。顺序即优先级，用于得分相同时的稳定排序。
TERMS: Sequence[Tuple[str, str]] = (
    ("brain state", "大脑状态"),
    ("state transition", "状态转换"),
    ("neural dynamics", "神经动力学"),
    ("attractor", "吸引子动力学"),
    ("metastability", "亚稳态"),
    ("criticality", "临界性"),
    ("energy landscape", "能量地形"),
    ("dynamical systems", "动力系统"),
    ("hidden markov model", "隐马尔可夫模型"),
    ("state space model", "状态空间模型"),
    ("dimensionality reduction", "降维"),
    ("neural manifold", "神经流形"),
    ("whole-brain model", "全脑模型"),
    ("functional connectivity", "功能连接"),
    ("structural connectivity", "结构连接"),
    ("resting-state", "静息态"),
    ("network neuroscience", "网络神经科学"),
    ("spiking network", "脉冲网络"),
    ("kuramoto", "Kuramoto 模型"),
    ("mean-field", "平均场"),
    ("eeg", "EEG"),
    ("meg", "MEG"),
    ("fmri", "fMRI"),
    ("bold", "BOLD 信号"),
    ("microstate", "微状态"),
    ("oscillation", "神经振荡"),
    ("signal processing", "信号处理"),
    ("time-frequency", "时频分析"),
    ("multimodal", "多模态融合"),
    ("model fitting", "模型拟合"),
    ("model comparison", "模型比较"),
    ("system identification", "系统辨识"),
    ("bayesian inference", "贝叶斯推断"),
    ("control theory", "控制理论"),
    ("closed-loop", "闭环调控"),
    ("neurostimulation", "神经调控"),
    ("deep brain stimulation", "深部脑刺激"),
    ("tms", "经颅磁刺激"),
    ("brain-computer interface", "脑机接口"),
    ("psychiatric disorder", "精神疾病"),
    ("depression", "抑郁"),
    ("schizophrenia", "精神分裂症"),
    ("biomarker", "生物标记"),
    ("consciousness", "意识"),
    ("anesthesia", "麻醉"),
    ("predictive coding", "预测编码"),
    ("reinforcement learning", "强化学习"),
    ("machine learning", "机器学习"),
    ("reproducibility", "可复现研究"),
)

#: 一个词都没认出来时用的兜底检索式（冷启动最常见的情况）。
FALLBACK_QUERIES: Sequence[str] = ("brain state dynamics", "neural dynamics computational model")


@lru_cache(maxsize=256)
def _pattern(term: str) -> re.Pattern[str]:
    body = r"\s+".join(re.escape(part) for part in term.split())
    return re.compile(rf"(?<![a-z0-9]){body}(?![a-z0-9])")


def count_terms(text: str) -> List[Tuple[str, int]]:
    """统计每个词在 ``text`` 里出现几次，按次数降序（同分保持词表顺序）。"""
    lowered = (text or "").lower()
    scored: List[Tuple[str, int]] = []
    for index, (term, _) in enumerate(TERMS):
        hits = len(_pattern(term).findall(lowered))
        if hits:
            scored.append((term, hits))
    order = {term: index for index, (term, _) in enumerate(TERMS)}
    scored.sort(key=lambda item: (-item[1], order[item[0]]))
    return scored


def pick_queries(text: str, limit: int = 4) -> List[str]:
    """从文本里挑出最该检索的几个方向；认不出来时返回兜底检索式。

    返回值之间用 ``|`` 连接就构成 ``week --topic`` 接受的多检索式格式。
    """
    found = [term for term, _ in count_terms(text)][: max(1, limit)]
    return found or list(FALLBACK_QUERIES)


def tag_titles(titles: Sequence[str]) -> List[Tuple[str, int]]:
    """把标题按词表归类，返回 ``[(中文标签, 篇数)]``（降序）。"""
    counts: Dict[str, int] = {}
    for title in titles:
        lowered = (title or "").lower()
        for term, label in TERMS:
            if _pattern(term).search(lowered):
                counts[label] = counts.get(label, 0) + 1
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))


def theme_line(titles: Sequence[str], limit: int = 8) -> str:
    """周报里的「这周涉及的主题」一行，例如 ``EEG×3、神经动力学×2``。"""
    tagged = tag_titles(titles)[:limit]
    return "、".join(f"{label}×{count}" for label, count in tagged) or "（未归类）"
