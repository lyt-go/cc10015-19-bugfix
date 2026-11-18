"""预测模型接口：模型配置、预测结果留档列表/详情、重新训练与导出。

V019 核对点：
- 列表先按筛选条件锁定完整训练批次，再返回该批次的预测点（不重新随机生成）；
- 详情只按预测点 id + 训练批次读取同一份留档结果；
- 重新训练失败由服务层整段回滚，接口把“最近完整批次”一起带回。
"""
from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app import predictions
from app.schemas import ActionPayload, ActionResult, EntryPayload, PageResult
from app.services.forecast_model import PREDICTION_FILTER_FIELDS, ForecastModelService

router = APIRouter(prefix="/api/forecast_model", tags=["预测模型"])
service = ForecastModelService()
LIST_FIELDS = ["模型编号", "预测对象", "历史窗口", "预测周期", "模型类型", "参数配置", "训练批次", "评估误差"]
STATUSES = ["草稿", "训练中", "已就绪", "已废弃"]
POINT_FIELDS = [
    "预测期", "模型编号", "预测对象", "业务周期", "训练批次", "指标编码",
    "指标值", "对比基准", "差异", "展示值", "精度位数", "指标口径",
]


def query_filters(request: Request) -> dict[str, str]:
    return {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in PREDICTION_FILTER_FIELDS and value.strip()
    }


def csv_cell(value: Any) -> str:
    """导出前做公式注入防护，其余交给 csv.writer 统一转义。"""
    text = str(value) if value is not None else ""
    return "'" + text if text.startswith(("=", "+", "-", "@")) else text


@router.get("/summary")
def get_summary() -> dict[str, Any]:
    """KPI 由留档数据推导，刷新不会换一组数。"""
    batches = [batch for batch in predictions.all_batches() if batch.get("完整")]
    models = service.list_results()
    return {"metrics": [
        {"label": "模型配置", "value": len(models), "trend": 0},
        {"label": "完整预测批次", "value": len(batches), "trend": 0},
        {"label": "留档预测点", "value": sum(int(batch.get("预测点数", 0)) for batch in batches), "trend": 0},
    ]}


@router.get("/predictions")
def list_predictions(
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None, description="已完成 / 失败 / 训练中"),
    policy: str | None = Query(default=predictions.POLICY_LATEST,
                              description="latest_complete 最近完成批次 / earliest_complete 最早完整批次"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> dict[str, Any]:
    """按训练批次稳定返回预测点；同一筛选条件重复查询结果一致。"""
    return service.list_predictions(
        filters=query_filters(request), keyword=keyword, status=status,
        policy=policy, page=page, size=size)


@router.get("/batches")
def list_batches() -> dict[str, Any]:
    """留档批次全量清单（含失败 / 训练中），供消费端对账和页面选择批次号。"""
    items = []
    for batch in sorted(predictions.all_batches(), key=predictions.batch_order):
        items.append({
            "id": batch.get("id"),
            "训练批次": batch.get("训练批次"),
            "模型编号": batch.get("模型编号"),
            "业务周期": batch.get("业务周期"),
            "状态": batch.get("状态"),
            "完整": bool(batch.get("完整")),
            "指标编码": batch.get("指标编码"),
            "精度位数": batch.get("精度位数"),
            "指标口径": batch.get("指标口径"),
            "预测点数": batch.get("预测点数"),
            "完成时间": batch.get("完成时间"),
            "错误": batch.get("错误") or "",
        })
    return {"items": items, "total": len(items), "checkedSpec": predictions.CHECK_SPEC}


@router.get("/predictions/{point_id}")
def get_prediction(
    point_id: int,
    batch: str | None = Query(default=None, description="训练批次，填写后只在该批次内查找"),
    policy: str | None = Query(default=predictions.POLICY_LATEST),
) -> dict[str, Any]:
    detail = service.get_prediction(point_id, batch_no=batch, policy=policy)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"预测点 {point_id} 不在任何完整训练批次中")
    return detail


@router.get("/export")
def export_entries(request: Request, policy: str | None = Query(default=predictions.POLICY_LATEST)) -> StreamingResponse:
    """导出当前筛选条件命中的预测点，列里始终带训练批次和指标口径。"""
    payload = service.list_predictions(
        filters=query_filters(request),
        keyword=request.query_params.get("keyword"),
        status=request.query_params.get("status"),
        policy=policy, page=1, size=1000000)
    buf = io.StringIO(newline="")
    writer = csv.writer(buf)
    writer.writerow(["id", *POINT_FIELDS])
    for item in payload["items"]:
        writer.writerow([csv_cell(item.get(key, "")) for key in ["id", *POINT_FIELDS]])
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": "attachment; filename=forecast_model.csv"})


@router.get("", response_model=PageResult[dict])
def list_models(
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    filters = {
        key: value.strip() for key, value in request.query_params.items()
        if key in LIST_FIELDS and value.strip()
    }
    items, total = service.list_entries(
        filters=filters, keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.post("/run")
def run_analysis(payload: EntryPayload) -> dict[str, Any]:
    results = service.run_analysis(payload.values)
    return {"items": results}


@router.post("/{entry_id}/retrain", response_model=ActionResult)
def retrain(entry_id: int, payload: EntryPayload) -> ActionResult:
    """重新训练：失败整体回滚，返回最近一次完整批次供页面继续复核。"""
    result = service.retrain(entry_id, payload.values)
    return ActionResult(
        ok=bool(result.get("ok")),
        message=str(result.get("message") or ""),
        entry=result.get("batch") or result.get("lastBatch"),
    )


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    entry, message = service.run_action(entry_id, payload.action.strip())
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/{entry_id}")
def get_entry(entry_id: int) -> dict:
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"预测模型 {entry_id} 不存在")
    detail = dict(entry)
    service._attach_confirmed_batch(detail)
    return detail
