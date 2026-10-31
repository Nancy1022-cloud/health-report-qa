"""指标列表的成员、指标、日期闭区间输入契约。"""
from datetime import date
from pydantic import BaseModel, ConfigDict, Field, model_validator

class MetricFilters(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    member_id: str = Field(min_length=1, max_length=80)
    metric_code: str | None = Field(default=None, min_length=1, max_length=80)
    date_from: date | None = None
    date_to: date | None = None

    @model_validator(mode="after")
    def ordered_dates(self):
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("date_from 不能晚于 date_to")
        return self
