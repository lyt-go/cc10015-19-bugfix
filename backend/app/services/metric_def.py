"""指标定义业务规则：状态流转、字段校验与筛选口径都收在这里。

V019：列表/详情会读取预测结果留档中该指标最近完成批次的指标编码与精度位数，
按当前列表的筛选条件复核展示值；预测端没有完成批次时退回指标定义自身配置。
"""
from __future__ import annotations

from typing import Any

from app import predictions
from app.store import store

MODULE = "metric_def"
LIST_FIELDS = ["指标编号", "指标名称", "计算公式", "统计粒度", "时间窗口", "基准取值", "精度位数", "口径版本"]
REQUIRED_FIELDS = ["指标编号", "指标名称", "计算公式"]
STATUS_ORDER = ["草稿", "待校验", "已生效", "已废弃"]
ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "标记废弃": "已废弃"}
NEGATIVE_ACTIONS = []


class MetricDefService:
    def _attach_forecast_check(self, row: dict[str, Any]) -> bool:
        """读取预测留档的确认结果，校验精度口径是否一致。返回是否通过核对。"""
        code = str(row.get("指标编号") or "")
        batch = predictions.confirmed_batch_for_metric(code)
        if batch is None:
            row["预测训练批次"] = ""
            row["预测精度位数"] = None
            row["预测指标口径"] = ""
            row["口径核对"] = "无完整批次"
            return True
        precision = predictions.precision_of(row)
        forecast_precision = int(batch.get("精度位数", precision) or precision)
        forecast_calibre = str(batch.get("指标口径") or "")
        calibre = str(row.get("口径版本") or "")
        ok = forecast_precision == precision and (not forecast_calibre or forecast_calibre == calibre)
        row["预测训练批次"] = batch.get("训练批次")
        row["预测精度位数"] = forecast_precision
        row["预测指标口径"] = forecast_calibre
        row["预测点数"] = batch.get("预测点数")
        row["口径核对"] = "一致" if ok else "精度或口径不一致"
        return ok

    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = [dict(row) for row in store.rows(MODULE)]
        for field, expected in (filters or {}).items():
            rows = [row for row in rows if expected.casefold() in str(row.get(field, "")).casefold()]
        # 先按列表筛选条件缩小范围，再按这些行的指标编码去预测留档核对展示值。
        for row in rows:
            self._attach_forecast_check(row)
        if keyword:
            rows = [row for row in rows if keyword.casefold() in str(row.get("指标编号", "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        row = store.find(MODULE, entry_id)
        if row is None:
            return None
        detail = dict(row)
        self._attach_forecast_check(detail)
        return detail

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in LIST_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"指标定义 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于指标定义可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"指标定义已{action}"
