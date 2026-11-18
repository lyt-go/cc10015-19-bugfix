"""V019 预测结果留档：稳定取数、批次绑定、事务回滚与跨模块口径核对。"""
import copy
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import predictions
from app.seed import SEED_ROWS
from app.services.forecast_model import PREDICTION_MODULE, ForecastModelService
from app.services.metric_def import MetricDefService
from app.store import store


def _reset() -> ForecastModelService:
    store.rows("forecast_model")[:] = [dict(row) for row in SEED_ROWS["forecast_model"]]
    store.rows(PREDICTION_MODULE)[:] = copy.deepcopy(SEED_ROWS["forecast_prediction"])
    store.rows("metric_def")[:] = [dict(row) for row in SEED_ROWS["metric_def"]]
    return ForecastModelService()


def _point_ids(payload):
    return [point["id"] for point in payload["items"]]


def test_repeated_query_returns_same_points():
    """连续刷新同一筛选条件必须得到相同预测点，且始终绑定同一训练批次。"""
    service = _reset()
    first = service.list_predictions()
    second = service.list_predictions()
    assert first["trainingBatch"] == second["trainingBatch"]
    assert _point_ids(first) == _point_ids(second)
    assert first["items"] == second["items"]
    assert first["trainingBatch"] and first["metricCode"] and first["calibre"]


def test_filter_by_training_batch_is_stable():
    service = _reset()
    filtered = service.list_predictions(filters={"训练批次": "FB-202609-01"})
    assert filtered["total"] == 6
    assert {point["训练批次"] for point in filtered["items"]} == {"FB-202609-01"}
    assert filtered["trainingBatch"] == "FB-202609-01"
    assert _point_ids(filtered) == _point_ids(service.list_predictions(
        filters={"训练批次": "FB-202609-01"}))


def test_failed_and_partial_batches_never_return_points():
    """失败批次、训练中批次不参与筛选，避免落到半成品。"""
    service = _reset()
    failed = service.list_predictions(filters={"训练批次": "FB-202609-03F"})
    assert failed["total"] == 0
    assert failed["items"] == []
    assert "尚未完成" in failed["message"]

    partial = service.list_predictions(filters={"训练批次": "FB-202609-12"})
    assert partial["total"] == 0
    assert "半成品批次不参与查询" in partial["message"]

    by_status = service.list_predictions(status="训练中")
    assert by_status["total"] == 0
    assert "只返回已完成批次" in by_status["message"]


def test_conflict_resolution_by_policy():
    """同一业务周期两个完整批次：最近完成批次附旧批次对照，最早完整批次稳定直返。"""
    service = _reset()
    latest = service.list_predictions(filters={"模型编号": "FORE-0001"})
    assert latest["conflict"] is True
    assert latest["trainingBatch"] == "FB-202609-02"
    assert latest["comparison"]["训练批次"] == "FB-202609-01"
    assert latest["comparison"]["预测点"]

    earliest = service.list_predictions(
        filters={"模型编号": "FORE-0001"}, policy="earliest_complete")
    assert earliest["conflict"] is True
    assert earliest["trainingBatch"] == "FB-202609-01"
    assert earliest["comparison"] is None


def test_list_and_detail_read_same_batch():
    service = _reset()
    payload = service.list_predictions()
    point = payload["items"][0]
    detail = service.get_prediction(point["id"], batch_no=payload["trainingBatch"])
    assert detail is not None
    assert detail["batch"]["训练批次"] == payload["trainingBatch"]
    assert detail["point"] == point
    assert detail["displayCheck"]["ok"] is True
    # 指定另一批次时找不到该点，不允许跨批次串数据。
    assert service.get_prediction(point["id"], batch_no="FB-202609-01") is None


def test_display_value_follows_metric_precision():
    """指标定义精度位数 2 位时，预测点展示值保留 2 位小数。"""
    service = _reset()
    payload = service.list_predictions(filters={"指标编码": "METR-0001"})
    assert payload["precision"] == 2
    for point in payload["items"]:
        assert point["精度位数"] == 2
        assert len(point["展示值"].split(".")[1]) == 2


def test_retrain_failure_rolls_back_and_keeps_last_complete():
    service = _reset()
    before = service.list_predictions(filters={"模型编号": "FORE-0001"})
    batch_count = len(store.rows(PREDICTION_MODULE))

    result = service.retrain(1, {"业务周期": "2026-11", "预测点数": "4", "模拟失败": "true"})
    assert result["ok"] is False
    assert result["lastBatch"]["训练批次"] == before["trainingBatch"]
    # 事务整体回滚：不留下“训练中”或半截批次。
    assert len(store.rows(PREDICTION_MODULE)) == batch_count
    after = service.list_predictions(filters={"模型编号": "FORE-0001"})
    assert after["trainingBatch"] == before["trainingBatch"]
    assert _point_ids(after) == _point_ids(before)
    assert all(batch.get("状态") != "训练中" or batch.get("id") == 5
               for batch in store.rows(PREDICTION_MODULE))


def test_retrain_success_creates_new_complete_batch():
    service = _reset()
    result = service.retrain(1, {"业务周期": "2026-11", "预测点数": "4"})
    assert result["ok"] is True
    batch_no = result["batch"]["训练批次"]
    assert result["batch"]["完整"] is True

    payload = service.list_predictions(filters={"训练批次": batch_no})
    assert payload["total"] == 4
    assert payload["trainingBatch"] == batch_no
    assert all(point["训练批次"] == batch_no for point in payload["items"])
    # 成功训练不会留下同一批次号的“训练中”占位记录。
    assert [batch["训练批次"] for batch in store.rows(PREDICTION_MODULE)].count(batch_no) == 1
    # 默认“最近完成批次”切到新批次，旧批次仍可按批次号复核。
    default_view = service.list_predictions()
    assert default_view["trainingBatch"] == batch_no
    old_view = service.list_predictions(filters={"训练批次": "FB-202609-02"})
    assert old_view["total"] == 6


def test_metric_def_reads_confirmed_forecast_batch():
    """指标定义按列表筛选条件读取预测留档的指标编码与精度位数并核对展示值。"""
    _reset()
    metric_service = MetricDefService()
    rows, total = metric_service.list_entries(filters={"指标编号": "METR-0002"})
    assert total == 1
    row = rows[0]
    assert row["预测训练批次"] == "FB-202609-11"
    assert row["预测精度位数"] == int(row["精度位数"])
    assert row["预测指标口径"] == row["口径版本"]
    assert row["口径核对"] == "一致"

    detail = metric_service.get_entry(2)
    assert detail["预测训练批次"] == "FB-202609-11"
    assert detail["口径核对"] == "一致"


def test_metric_def_reports_mismatch_after_precision_change():
    _reset()
    store.rows("metric_def")[1]["精度位数"] = "5"
    row = MetricDefService().list_entries(filters={"指标编号": "METR-0002"})[0][0]
    assert row["口径核对"] == "精度或口径不一致"


def test_batch_order_is_deterministic_without_completion_time():
    """完成时间相同时用 id 兜底，排序结果不会随插入顺序抖动。"""
    _reset()
    service = ForecastModelService()
    first = service.list_predictions(filters={"模型编号": "FORE-0002"})
    second = service.list_predictions(filters={"模型编号": "FORE-0002"})
    assert first["trainingBatch"] == second["trainingBatch"] == "FB-202609-11"
    assert predictions.batch_order({"完成时间": "", "id": 3}) == ("", 3)
