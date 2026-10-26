"""M6 的核对格式：模型只给候选值，confirmed 由服务端在事务中设置。"""

import hashlib
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_TEXT = 8000
CATALOG = {
    "空腹血糖": "fasting_glucose", "总胆固醇": "total_cholesterol",
    "甘油三酯": "triglycerides", "尿酸": "uric_acid",
    "糖化血红蛋白": "hba1c", "低密度脂蛋白胆固醇": "ldl_cholesterol",
    "高密度脂蛋白胆固醇": "hdl_cholesterol",
}


def code_for(name):
    name = name.strip()
    return CATALOG.get(name, "custom_" + hashlib.sha256(name.encode()).hexdigest()[:16])


class Metric(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    metric_code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,79}$")
    metric_name: str = Field(min_length=1, max_length=80)
    value: str = Field(min_length=1, max_length=60)
    unit: str = Field(min_length=1, max_length=40)
    reference_range: str | None = Field(default=None, max_length=150)
    source_text: str = Field(min_length=1, max_length=1000)

    @field_validator("value")
    @classmethod
    def numeric(cls, value):
        if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)", value):
            raise ValueError("数值需为明确的十进制数字；<、>等定性结果保留在原文，不存为精确指标。")
        try:
            number = Decimal(value)
        except InvalidOperation:
            raise ValueError("无效数值。") from None
        if not number.is_finite():
            raise ValueError("数值必须有限。")
        return format(number.normalize(), "f")


class Review(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=160)
    report_date: date
    report_type: str = Field(min_length=1, max_length=80)
    raw_text: str = Field(min_length=1, max_length=MAX_TEXT)
    metrics: list[Metric] = Field(max_length=80)
    reviewed: Literal[True]

    @field_validator("reviewed", mode="before")
    @classmethod
    def explicit_confirmation(cls, value):
        if value is not True:
            raise ValueError("请明确勾选人工核对确认。")
        return value

    @model_validator(mode="after")
    def check_sources(self):
        if not self.title.strip() or not self.report_type.strip() or not self.raw_text.strip():
            raise ValueError("标题、类型和核对文本不能为空。")
        codes = [m.metric_code for m in self.metrics]
        if len(codes) != len(set(codes)):
            raise ValueError("同一份报告不能有重复 metric_code；请核对是否重复抽取。")
        for m in self.metrics:
            if m.source_text not in self.raw_text:
                raise ValueError(f"{m.metric_name}：来源原句必须出现在核对文本中。")
            numbers = re.findall(r"(?<![\d.])[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?![\d.])", m.source_text)
            if Decimal(m.value) not in [Decimal(n) for n in numbers]:
                raise ValueError(f"{m.metric_name}：数值未出现在来源原句中。")
            if re.sub(r"\s+", "", m.unit) not in re.sub(r"\s+", "", m.source_text):
                raise ValueError(f"{m.metric_name}：单位未出现在来源原句中。")
        return self

    def canonical(self):
        data = self.model_dump(mode="json", exclude={"reviewed"})
        data["metrics"] = sorted(data["metrics"], key=lambda m: m["metric_code"])
        return data