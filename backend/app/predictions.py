"""预测结果留档（V019）：训练批次、指标口径与预测点的稳定生成规则。

这里只放纯函数与只读查询：
- 预测点由 ``模型编号 + 训练批次 + 业务周期 + 序号`` 派生，重复读取不会变值；
- 精度位数与指标口径从指标定义里读取，并随预测点一起留档；
- 指标定义模块通过 ``confirmed_batch_for_metric`` 读取已确认批次，避免两边口径漂移。

阈值与取整规则集中在这个文件里，路由层和服务层都不再做二次加工。
"""
from __future__ import annotations

import hashlib
from typing import Any

PREDICTION_MODULE = "forecast_prediction"
CHECK_SPEC = "V019"

POLICY_LATEST = "latest_complete"
POLICY_EARLIEST = "earliest_complete"
POLICY_LABELS = {
    POLICY_LATEST: "最近完成批次",
    POLICY_EARLIEST: "最早完整批次",
}

_POLICY_ALIASES: dict[str, str] = {
    "latest": POLICY_LATEST,
    "latest_complete": POLICY_LATEST,
    "最近的完整批次": POLICY_LATEST,
    "最近完成": POLICY_LATEST,
    "最近完成批次": POLICY_LATEST,
    "earliest": POLICY_EARLIEST,
    "earliest_complete": POLICY_EARLIEST,
    "最早完成": POLICY_EARLIEST,
    "最早完成批次": POLICY_EARLIEST,
    "最早完整": POLICY_EARLIEST,
    "最早完整批次": POLICY_EARLIEST,
}


def normalize_policy(value: Any) -> str:
    """把页面或接口传进来的批次策略收敛成两个稳定枚举值。"""
    key = str(value or "").strip().casefold()
    return _POLICY_ALIASES.get(key, POLICY_LATEST)


def truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().casefold() in {"1", "true", "yes", "y", "是", "失败", "模拟失败"}


def batch_order(batch: dict[str, Any]) -> tuple[str, int]:
    """完成时间优先、id 兜底，保证同一批数据的排序始终一致。"""
    return str(batch.get("完成时间") or ""), int(batch.get("id", 0) or 0)


def precision_of(metric: dict[str, Any] | None, fallback: int = 3) -> int:
    raw = (metric or {}).get("精度位数")
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        value = fallback
    return max(0, min(value, 6))


def format_value(value: Any, precision: int) -> str:
    try:
        return f"{float(value):.{precision}f}"
    except (TypeError, ValueError):
        return "—"


def metric_code_of(metric: dict[str, Any] | None) -> str:
    return str((metric or {}).get("指标编号") or "")


def calibre_of(metric: dict[str, Any] | None) -> str:
    return str((metric or {}).get("口径版本") or "v1")


def metric_for_model(
    model: dict[str, Any],
    metrics: list[dict[str, Any]],
    index: int = 0,
) -> dict[str, Any] | None:
    """按指标编码 / 编号后缀 / 位置依次匹配模型对应的指标定义。

    真实项目里会有显式的模型-指标关联表；当前仓库用确定性匹配，
    保证刷新、重启后同一模型拿到的精度位数和口径不会换。
    """
    if not metrics:
        return None
    declared = str(model.get("指标编码") or "").strip()
    if declared:
        for metric in metrics:
            if str(metric.get("指标编号") or "").strip() == declared:
                return metric
    model_code = str(model.get("模型编号") or "")
    digits = "".join(char for char in reversed(model_code) if char.isdigit())[::-1]
    if digits:
        for metric in metrics:
            if str(metric.get("指标编号") or "").endswith(digits):
                return metric
    return metrics[index % len(metrics)]


def _deterministic_pair(model_code: str, batch_no: str, period: str, index: int) -> tuple[float, float]:
    digest = hashlib.sha256(f"{model_code}|{batch_no}|{period}|{index}".encode("utf-8")).hexdigest()

    def unit(offset: int) -> float:
        return int(digest[offset:offset + 8], 16) / 0xFFFFFFFF

    return 0.65 + unit(0) * 0.30, 0.60 + unit(8) * 0.30


def point_payload(
    *,
    model: dict[str, Any],
    batch_no: str,
    period: str,
    index: int,
    point_id: int,
    value: float,
    base: float,
    metric: dict[str, Any] | None,
) -> dict[str, Any]:
    """构造一条预测点，精度位数和指标口径随点一起落档。"""
    precision = precision_of(metric)
    rounded_value = round(float(value), precision)
    rounded_base = round(float(base), precision)
    return {
        "id": point_id,
        "序号": index + 1,
        "预测期": f"{period}-P{index + 1:02d}",
        "模型id": model.get("id"),
        "模型编号": str(model.get("模型编号") or ""),
        "预测对象": str(model.get("预测对象") or ""),
        "业务周期": period,
        "训练批次": batch_no,
        "指标编码": metric_code_of(metric),
        "指标值": rounded_value,
        "对比基准": rounded_base,
        "差异": round(rounded_value - rounded_base, precision),
        "展示值": format_value(rounded_value, precision),
        "精度位数": precision,
        "指标口径": calibre_of(metric),
    }


def build_points(
    model: dict[str, Any],
    batch_no: str,
    period: str,
    count: int,
    metric: dict[str, Any] | None,
    *,
    start_id: int,
) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    for index in range(max(0, count)):
        value, base = _deterministic_pair(
            str(model.get("模型编号") or ""), batch_no, period, index)
        points.append(point_payload(
            model=model,
            batch_no=batch_no,
            period=period,
            index=index,
            point_id=start_id + index,
            value=value,
            base=base,
            metric=metric,
        ))
    return points


def build_seed_batches(
    models: list[dict[str, Any]],
    metrics: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """服务启动时预置几个留档批次，覆盖稳定取数与版本冲突两种场景。

    同一业务周期放两个完整批次（FB-202609-01 / 02），再放一个失败批次，
    让“最近完成批次”不会落到失败或训练中的半成品上。
    """
    if not models:
        return []
    specs = [
        # model_index, period, batch_no, status, complete, created, finished, point_count, error, partial
        (0, "2026-09", "FB-202609-01", "已完成", True, "2026-09-05T09:00:00", "2026-09-05T09:12:00", 6, "", False),
        (0, "2026-09", "FB-202609-02", "已完成", True, "2026-09-18T10:00:00", "2026-09-18T10:20:00", 6, "", False),
        (0, "2026-09", "FB-202609-03F", "失败", False, "2026-09-20T08:00:00", "", 0, "训练任务异常退出", False),
        (1, "2026-09", "FB-202609-11", "已完成", True, "2026-09-10T09:00:00", "2026-09-10T09:15:00", 6, "", False),
        (1, "2026-09", "FB-202609-12", "训练中", False, "2026-09-21T14:00:00", "", 2, "", True),
        (2, "2026-10", "FB-202610-01", "已完成", True, "2026-10-08T11:00:00", "2026-10-08T11:18:00", 6, "", False),
    ]
    batches: list[dict[str, Any]] = []
    next_id = 1
    for (model_index, period, batch_no, status, complete,
         created, finished, point_count, error, partial) in specs:
        if model_index >= len(models):
            continue
        model = models[model_index]
        metric = metric_for_model(model, metrics, model_index)
        points = build_points(
            model, batch_no, period, point_count, metric, start_id=next_id * 1000 + 1)
        if partial:
            points = points[:2]
        batches.append({
            "id": next_id,
            "模型id": model.get("id"),
            "模型编号": str(model.get("模型编号") or ""),
            "预测对象": str(model.get("预测对象") or ""),
            "业务周期": period,
            "训练批次": batch_no,
            "状态": status,
            "完整": complete,
            "指标编码": metric_code_of(metric),
            "精度位数": precision_of(metric),
            "指标口径": calibre_of(metric),
            "创建时间": created,
            "完成时间": finished,
            "预测点数": len(points),
            "错误": error,
            "points": points,
        })
        next_id += 1
    return batches


# ---------------------------------------------------------------------------
# 只读查询：指标定义模块从这里读取已确认批次的指标编码与精度位数。
# ---------------------------------------------------------------------------

def _store_rows(module: str) -> list[dict[str, Any]]:
    # 延迟导入，避免 store 初始化时反向依赖本模块。
    from app.store import store

    return store.rows(module)


def all_batches() -> list[dict[str, Any]]:
    return _store_rows(PREDICTION_MODULE)


def confirmed_batch_for_metric(
    metric_code: str,
    policy: str = POLICY_LATEST,
) -> dict[str, Any] | None:
    """返回该指标最近/最早完成的完整批次；半成品批次永远不会被选中。"""
    code = str(metric_code or "").strip()
    if not code:
        return None
    policy = normalize_policy(policy)
    batches = [
        batch for batch in all_batches()
        if batch.get("完整") and str(batch.get("指标编码") or "") == code and batch.get("points")
    ]
    if not batches:
        return None
    batches.sort(key=batch_order)
    return batches[0] if policy == POLICY_EARLIEST else batches[-1]


def validate_point_display(point: dict[str, Any]) -> tuple[str, str, bool]:
    """按留档精度重新格式化一次，确认列表展示值与落档值一致。"""
    precision = int(point.get("精度位数", 3) or 3)
    expected = format_value(point.get("指标值"), precision)
    actual = str(point.get("展示值") or "")
    return expected, actual, expected == actual
