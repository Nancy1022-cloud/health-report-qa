"""数据契约规则；不按临床参考范围删除数值，不做隐式单位换算。"""

import hashlib
import json

from app.upload_models import CATALOG

UNITS = {
    "fasting_glucose": {"mmol/L", "mg/dL"},
    "total_cholesterol": {"mmol/L", "mg/dL"},
    "triglycerides": {"mmol/L", "mg/dL"},
    "uric_acid": {"umol/L", "mg/dL"},
    "hba1c": {"%", "mmol/mol"},
    "ldl_cholesterol": {"mmol/L", "mg/dL"},
    "hdl_cholesterol": {"mmol/L", "mg/dL"},
}


def normalized_unit(value):
    return "".join(value.split()).replace("μ", "u").replace("µ", "u")


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def review_issues(review):
    """Review 已检查日期、必填、重复 code、数值和来源；这里补名码与单位。"""
    issues = []
    reverse = {code: name for name, code in CATALOG.items()}
    for index, metric in enumerate(review.metrics):
        known = CATALOG.get(metric.metric_name)
        if (known is not None and known != metric.metric_code or
                metric.metric_code in reverse and reverse[metric.metric_code] != metric.metric_name):
            issues.append({"field": f"metrics.{index}.metric_code", "rule": "metric_name_code", "severity": "error"})
        allowed = UNITS.get(metric.metric_code)
        if allowed and normalized_unit(metric.unit) not in allowed:
            issues.append({"field": f"metrics.{index}.unit", "rule": "metric_unit", "severity": "error"})
        if allowed is None:
            issues.append({"field": f"metrics.{index}.unit", "rule": "custom_metric_manual_review", "severity": "warning"})
    return issues
