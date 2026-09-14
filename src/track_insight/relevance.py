import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from track_insight.enums import RelevanceLabel


@dataclass(frozen=True, slots=True)
class RelevanceRules:
    version: str
    exclusions: dict[str, list[str]]
    signals: dict[str, list[str]]
    industries: dict[str, list[str]]
    fingerprint: str


@dataclass(frozen=True, slots=True)
class RelevanceResult:
    label: RelevanceLabel
    score: float
    signal_types: list[str]
    industries: list[str]
    matched_positive_rules: list[str]
    matched_negative_rules: list[str]
    reason: str
    evidence: list[str]


def load_relevance_rules(path: Path = Path("config/relevance_rules.yaml")) -> RelevanceRules:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return RelevanceRules(
        version=str(payload["version"]),
        exclusions=payload.get("exclusions", {}),
        signals=payload.get("signals", {}),
        industries=payload.get("industries", {}),
        fingerprint=hashlib.sha256(canonical.encode()).hexdigest(),
    )


def classify_relevance(title: str | None, text: str, rules: RelevanceRules) -> RelevanceResult:
    title_text = title or ""
    combined = f"{title_text}\n{text}"
    negatives = _matches(title_text, rules.exclusions)
    signals = _matches(combined, rules.signals)
    industries = _matches(combined, rules.industries)
    positive_rules = [f"{group}:{term}" for group, term in signals]
    negative_rules = [f"{group}:{term}" for group, term in negatives]
    signal_types = _unique(group for group, _ in signals)
    industry_names = _unique(group for group, _ in industries)

    if negatives and not signals:
        label = RelevanceLabel.IRRELEVANT
        score = 0.95
        reason = "标题命中高置信无关类型，且没有发现产业事件信号。"
    elif signals and (industries or len(signal_types) >= 2):
        label = RelevanceLabel.RELEVANT
        score = min(0.98, 0.78 + 0.05 * len(signal_types) + 0.03 * len(industry_names))
        reason = "同时发现明确事件信号和产业语义，可进入事件抽取。"
    elif signals:
        label = RelevanceLabel.POSSIBLY_RELEVANT
        score = 0.62
        reason = "发现事件信号，但产业对象或经营影响尚不明确。"
    elif industries:
        label = RelevanceLabel.POSSIBLY_RELEVANT
        score = 0.52
        reason = "发现产业语义，但没有识别出明确发生的事件。"
    else:
        label = RelevanceLabel.POSSIBLY_RELEVANT
        score = 0.4
        reason = "规则无法明确判断，保留供语义分类或人工复核。"

    terms = [term for _, term in signals + industries + negatives]
    return RelevanceResult(
        label=label,
        score=round(score, 3),
        signal_types=signal_types,
        industries=industry_names,
        matched_positive_rules=positive_rules,
        matched_negative_rules=negative_rules,
        reason=reason,
        evidence=_evidence(combined, terms),
    )


def _matches(text: str, groups: dict[str, list[str]]) -> list[tuple[str, str]]:
    return [
        (group, term)
        for group, terms in groups.items()
        for term in terms
        if re.search(re.escape(term), text, re.I)
    ]


def _evidence(text: str, terms: list[str], limit: int = 3) -> list[str]:
    evidence: list[str] = []
    for term in terms:
        match = re.search(re.escape(term), text, re.I)
        if not match:
            continue
        start = max(0, match.start() - 45)
        end = min(len(text), match.end() + 75)
        snippet = " ".join(text[start:end].split())
        if snippet not in evidence:
            evidence.append(snippet)
        if len(evidence) >= limit:
            break
    return evidence


def _unique(values: Any) -> list[str]:
    return list(dict.fromkeys(values))
