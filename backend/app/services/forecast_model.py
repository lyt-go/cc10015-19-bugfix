"""预测模型业务规则：模型配置、预测留档批次与稳定取数口径。

V019 的核心约束：
- 预测点只在训练完成时写入 ``forecast_prediction``，读取时不再随机生成；
- 列表查询必须先锁定一个“完整批次”，半成品 / 失败批次永远不会返回预测点；
- 训练批次号、指标编码、精度位数、指标口径随批次和预测点一起留档；
- 重新训练走整段事务：失败整体回滚，最近一次完整预测结果保持不变。
"""
from __future__ import annotations

import hashlib
import json
import time
from datetime import date
from typing import Any

from app import predictions
from app.store import store

MODULE = "forecast_model"
PREDICTION_MODULE = predictions.PREDICTION_MODULE
REQUIRED_FIELDS = ["模型编号", "预测对象", "历史窗口"]
LIST_FIELDS = ["模型编号", "预测对象", "历史窗口", "预测周期", "模型类型", "参数配置", "训练批次", "评估误差"]
STATUS_ORDER = ["草稿", "训练中", "已就绪", "已废弃"]
ACTION_RULES = {"开始训练": "训练中", "确认就绪": "已就绪", "废弃模型": "已废弃"}
NEGATIVE_ACTIONS = []

PREDICTION_FILTER_FIELDS = ["模型编号", "预测对象", "业务周期", "训练批次", "指标编码", "指标口径"]
KEYWORD_FIELDS = ["模型编号", "预测对象", "训练批次", "指标编码", "预测期"]


def _matches(row: dict[str, Any], filters: dict[str, str]) -> bool:
    for field, expected in filters.items():
        if expected.casefold() not in str(row.get(field, "")).casefold():
            return False
    return True


def _complete(batch: dict[str, Any]) -> bool:
    return bool(batch.get("完整")) and bool(batch.get("points")) and str(batch.get("状态", "")) == "已完成"


def _batch_meta(batch: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": batch.get("id"),
        "训练批次": batch.get("训练批次"),
        "业务周期": batch.get("业务周期"),
        "模型id": batch.get("模型id"),
        "模型编号": batch.get("模型编号"),
        "预测对象": batch.get("预测对象"),
        "状态": batch.get("状态"),
        "完整": bool(batch.get("完整")),
        "指标编码": batch.get("指标编码"),
        "精度位数": batch.get("精度位数"),
        "指标口径": batch.get("指标口径"),
        "创建时间": batch.get("创建时间"),
        "完成时间": batch.get("完成时间"),
        "预测点数": batch.get("预测点数"),
        "错误": batch.get("错误") or "",
    }


def _comparison(batch: dict[str, Any], previous: dict[str, Any] | None) -> dict[str, Any] | None:
    """给出旧批次对照摘要 + 旧批次预测点，方便复核当前批次是否只是版本推进。"""
    if previous is None:
        return None
    precision = int(batch.get("精度位数", 3) or 3)
    current_points = batch.get("points") or []
    old_points = previous.get("points") or []
    current_avg = sum(float(p.get("指标值", 0)) for p in current_points) / len(current_points) if current_points else 0.0
    old_avg = sum(float(p.get("指标值", 0)) for p in old_points) / len(old_points) if old_points else 0.0
    return {
        **_batch_meta(previous),
        "平均指标值": round(old_avg, precision),
        "平均展示值": predictions.format_value(old_avg, precision),
        "与当前批次差异": round(current_avg - old_avg, precision),
        "差异展示值": predictions.format_value(current_avg - old_avg, precision),
        "预测点": old_points,
    }


class ForecastModelService:
    # ------------------------------------------------------------------
    # 模型配置
    # ------------------------------------------------------------------
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
            rows = [row for row in rows if _matches(row, {field: expected})]
        if keyword:
            rows = [row for row in rows if any(
                keyword.casefold() in str(row.get(field, "")).casefold()
                for field in ("模型编号", "预测对象"))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        for row in rows:
            self._attach_confirmed_batch(row)
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def _attach_confirmed_batch(self, model: dict[str, Any]) -> None:
        batches = [
            batch for batch in store.rows(PREDICTION_MODULE)
            if _complete(batch) and batch.get("模型id") == model.get("id")
        ]
        batches.sort(key=predictions.batch_order)
        latest = batches[-1] if batches else None
        model["最近完成批次"] = latest.get("训练批次") if latest else ""
        model["可用批次数"] = len(batches)
        model["指标编码"] = latest.get("指标编码") if latest else str(model.get("指标编码") or "")
        model["精度位数"] = latest.get("精度位数") if latest else None
        model["指标口径"] = latest.get("指标口径") if latest else ""

    def list_results(self) -> list[dict[str, Any]]:
        """兼容旧调用：返回模型配置清单，不再生成随机指标。"""
        rows, _ = self.list_entries(page=1, size=1000000)
        return rows

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def _metric_for(self, model: dict[str, Any]) -> dict[str, Any] | None:
        metrics = store.rows("metric_def")
        try:
            index = next(i for i, row in enumerate(store.rows(MODULE)) if row.get("id") == model.get("id"))
        except StopIteration:
            index = 0
        return predictions.metric_for_model(model, metrics, index)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [f for f in REQUIRED_FIELDS if not str(values.get(f) or "").strip()]
        if missing:
            return None, missing
        with store.lock:
            entry = {"id": store.next_id(MODULE)}
            for f in LIST_FIELDS:
                entry[f] = values.get(f, "")
            entry["指标编码"] = values.get("指标编码", "")
            entry["status"] = STATUS_ORDER[0]
            entry["pending"] = True
            entry["abnormal"] = False
            entry["created_at"] = time.time()
            store.rows(MODULE).append(entry)
            store.bump_version(MODULE)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"预测模型 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于预测模型可执行范围"
        target = ACTION_RULES[action]
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        store.bump_version(MODULE)
        return entry, f"预测模型已{action}"

    # ------------------------------------------------------------------
    # 预测结果列表 / 详情：先锁定完整批次，再返回该批次的预测点
    # ------------------------------------------------------------------
    def _matching_batches(
        self,
        filters: dict[str, str] | None,
        keyword: str | None,
        status: str | None,
    ) -> list[dict[str, Any]]:
        batches = list(store.rows(PREDICTION_MODULE))
        batch_filters = {k: v for k, v in (filters or {}).items() if k in PREDICTION_FILTER_FIELDS}
        if batch_filters:
            batches = [batch for batch in batches if _matches(batch, batch_filters)]
        if status:
            batches = [batch for batch in batches if str(batch.get("状态", "")) == status]
        if keyword:
            batches = [batch for batch in batches if any(
                keyword.casefold() in str(batch.get(field, "")).casefold()
                for field in ("模型编号", "预测对象", "训练批次", "指标编码"))]
        return batches

    def list_predictions(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        policy: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> dict[str, Any]:
        policy = predictions.normalize_policy(policy)
        filters = {k: v for k, v in (filters or {}).items() if k in PREDICTION_FILTER_FIELDS}
        batches = self._matching_batches(filters, keyword, status)
        complete = sorted([b for b in batches if _complete(b)], key=predictions.batch_order)

        context: dict[str, Any] = {
            "policy": policy,
            "policyLabel": predictions.POLICY_LABELS[policy],
            "checkedSpec": predictions.CHECK_SPEC,
            "filters": filters,
        }

        if not complete:
            context.update(self._empty_context(batches, filters, status))
            return {"items": [], "total": 0, "page": page, "size": size,
                    "batches": [], "conflict": False, "comparison": None, **context}

        selected = complete[0] if policy == predictions.POLICY_EARLIEST else complete[-1]
        same_period = [
            b for b in complete
            if b.get("模型id") == selected.get("模型id") and b.get("业务周期") == selected.get("业务周期")
        ]
        conflict = len(same_period) > 1
        previous = None
        if conflict:
            ordered = sorted(same_period, key=predictions.batch_order)
            position = ordered.index(selected)
            if policy == predictions.POLICY_LATEST and position > 0:
                previous = ordered[position - 1]
            elif policy == predictions.POLICY_EARLIEST and position + 1 < len(ordered):
                previous = ordered[position + 1]

        points = [dict(point) for point in selected.get("points") or []]
        points = [point for point in points if _matches(point, filters)]
        if keyword:
            points = [point for point in points if any(
                keyword.casefold() in str(point.get(field, "")).casefold() for field in KEYWORD_FIELDS)]
        total = len(points)
        start = (page - 1) * size
        context.update({
            "batch": _batch_meta(selected),
            "trainingBatch": selected.get("训练批次"),
            "metricCode": selected.get("指标编码"),
            "precision": selected.get("精度位数"),
            "calibre": selected.get("指标口径"),
        })
        return {
            "items": points[start:start + size],
            "total": total,
            "page": page,
            "size": size,
            "batches": [_batch_meta(batch) for batch in complete],
            "conflict": conflict,
            "comparison": _comparison(selected, previous) if policy == predictions.POLICY_LATEST else None,
            "message": (
                f"同一业务周期存在 {len(same_period)} 个完整批次，已按{predictions.POLICY_LABELS[policy]}返回"
                if conflict else ""
            ),
            **context,
        }

    def _empty_context(
        self,
        batches: list[dict[str, Any]],
        filters: dict[str, str],
        status: str | None,
    ) -> dict[str, Any]:
        requested = str(filters.get("训练批次") or "").strip()
        known = next(
            (batch for batch in store.rows(PREDICTION_MODULE)
             if batch.get("训练批次") == requested),
            None,
        ) if requested else None
        if requested and known is None:
            message = f"训练批次 {requested} 不存在，请按可用批次重新筛选"
        elif requested and not _complete(known):
            message = f"训练批次 {requested} 尚未完成，半成品批次不参与查询；已保留最近完整预测结果"
        elif requested:
            message = f"训练批次 {requested} 已完成，但与当前筛选条件不匹配，未返回预测点"
        elif status and status != "已完成":
            message = f"状态「{status}」的批次未形成完整预测结果，筛选只返回已完成批次"
        else:
            message = "暂无完整预测批次；训练失败或未完成时沿用最近一次完整预测结果"
        return {"message": message, "trainingBatch": requested, "metricCode": "",
                "precision": None, "calibre": "", "batch": None}

    def get_prediction(
        self,
        point_id: int,
        *,
        batch_no: str | None = None,
        policy: str | None = None,
    ) -> dict[str, Any] | None:
        policy = predictions.normalize_policy(policy)
        batches = [
            batch for batch in store.rows(PREDICTION_MODULE)
            if _complete(batch) and (not batch_no or batch.get("训练批次") == batch_no)
        ]
        batches.sort(key=predictions.batch_order)
        if not batches:
            return None
        for batch in batches:
            for point in batch.get("points") or []:
                if int(point.get("id", 0)) == point_id:
                    return self._point_detail(point, batch, policy)
        return None

    def _point_detail(self, point: dict[str, Any], batch: dict[str, Any], policy: str) -> dict[str, Any]:
        complete = sorted(
            [b for b in store.rows(PREDICTION_MODULE) if _complete(b)],
            key=predictions.batch_order,
        )
        same_period = [
            b for b in complete
            if b.get("模型id") == batch.get("模型id") and b.get("业务周期") == batch.get("业务周期")
        ]
        previous = None
        if len(same_period) > 1 and policy == predictions.POLICY_LATEST:
            ordered = sorted(same_period, key=predictions.batch_order)
            position = ordered.index(batch)
            previous = ordered[position - 1] if position > 0 else None
        expected, actual, ok = predictions.validate_point_display(point)
        return {
            "point": point,
            "batch": _batch_meta(batch),
            "comparison": _comparison(batch, previous) if policy == predictions.POLICY_LATEST else None,
            "conflict": len(same_period) > 1,
            "policy": policy,
            "policyLabel": predictions.POLICY_LABELS[policy],
            "checkedSpec": predictions.CHECK_SPEC,
            "displayCheck": {"expected": expected, "actual": actual, "ok": ok},
        }

    # ------------------------------------------------------------------
    # 训练 / 重新训练：整段事务，失败回滚，不留半成品
    # ------------------------------------------------------------------
    def _next_batch_no(self, period: str) -> str:
        stem = f"FB-{period.replace('-', '')}"
        existing = {str(batch.get("训练批次") or "") for batch in store.rows(PREDICTION_MODULE)}
        sequence = 1
        while f"{stem}-{sequence:02d}" in existing:
            sequence += 1
        return f"{stem}-{sequence:02d}"

    def _current_period(self) -> str:
        batches = store.rows(PREDICTION_MODULE)
        if batches:
            latest = max(batches, key=predictions.batch_order)
            if latest.get("业务周期"):
                return str(latest["业务周期"])
        return date.today().strftime("%Y-%m")

    def _persist_points(
        self,
        model: dict[str, Any],
        batch_no: str,
        period: str,
        points: list[dict[str, Any]],
        *,
        status: str,
        complete: bool,
        error: str = "",
    ) -> dict[str, Any]:
        batch_id = store.next_id(PREDICTION_MODULE)
        metric = self._metric_for(model)
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        stored = []
        for index, point in enumerate(points):
            stored.append({
                "id": batch_id * 1000 + index + 1,
                "序号": index + 1,
                "预测期": str(point.get("预测期") or f"{period}-P{index + 1:02d}"),
                "模型id": model.get("id"),
                "模型编号": str(model.get("模型编号") or ""),
                "预测对象": str(model.get("预测对象") or ""),
                "业务周期": period,
                "训练批次": batch_no,
                "指标编码": predictions.metric_code_of(metric),
                "指标值": point.get("指标值"),
                "对比基准": point.get("对比基准"),
                "差异": point.get("差异"),
                "展示值": point.get("展示值"),
                "精度位数": predictions.precision_of(metric),
                "指标口径": predictions.calibre_of(metric),
            })
        batch = {
            "id": batch_id,
            "模型id": model.get("id"),
            "模型编号": str(model.get("模型编号") or ""),
            "预测对象": str(model.get("预测对象") or ""),
            "业务周期": period,
            "训练批次": batch_no,
            "状态": status,
            "完整": complete,
            "指标编码": predictions.metric_code_of(metric),
            "精度位数": predictions.precision_of(metric),
            "指标口径": predictions.calibre_of(metric),
            "创建时间": now,
            "完成时间": now if complete else "",
            "预测点数": len(stored),
            "错误": error,
            "points": stored,
        }
        store.rows(PREDICTION_MODULE).append(batch)
        return batch

    def _complete_batch(self, batch: dict[str, Any], points: list[dict[str, Any]]) -> None:
        """把已占位的“训练中”批次补全为已完成，训练批次号保持唯一。"""
        stored = []
        for index, point in enumerate(points):
            stored.append({
                "id": int(batch["id"]) * 1000 + index + 1,
                "序号": index + 1,
                "预测期": str(point.get("预测期") or f"{batch.get('业务周期')}-P{index + 1:02d}"),
                "模型id": batch.get("模型id"),
                "模型编号": batch.get("模型编号"),
                "预测对象": batch.get("预测对象"),
                "业务周期": batch.get("业务周期"),
                "训练批次": batch.get("训练批次"),
                "指标编码": batch.get("指标编码"),
                "指标值": point.get("指标值"),
                "对比基准": point.get("对比基准"),
                "差异": point.get("差异"),
                "展示值": point.get("展示值"),
                "精度位数": batch.get("精度位数"),
                "指标口径": batch.get("指标口径"),
            })
        batch.update({
            "状态": "已完成",
            "完整": True,
            "预测点数": len(stored),
            "完成时间": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "错误": "",
            "points": stored,
        })

    def _last_complete(self, model: dict[str, Any]) -> dict[str, Any] | None:
        batches = [
            batch for batch in store.rows(PREDICTION_MODULE)
            if _complete(batch) and batch.get("模型id") == model.get("id")
        ]
        batches.sort(key=predictions.batch_order)
        return batches[-1] if batches else None

    def retrain(self, model_id: int, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """重新训练一个模型。成功才落新批次；失败整体回滚并保留最近完整结果。"""
        params = params or {}
        model = self.get_entry(model_id)
        if model is None:
            return {"ok": False, "message": f"预测模型 {model_id} 不存在", "lastBatch": None}

        period = str(params.get("业务周期") or model.get("业务周期") or self._current_period())
        try:
            count = int(params.get("预测点数") or model.get("预测周期") or 3)
        except (TypeError, ValueError):
            count = 3
        count = max(1, min(count, 60))
        metric = self._metric_for(model)

        with store.lock:
            snapshot = store.snapshot(PREDICTION_MODULE, MODULE)
            try:
                batch_no = str(params.get("训练批次") or "").strip() or self._next_batch_no(period)
                # 先落一条“训练中”的批次，模拟真实训练写入；失败时靠快照整体回滚。
                pending = self._persist_points(
                    model, batch_no, period, [], status="训练中", complete=False)
                if predictions.truthy(params.get("模拟失败")) or predictions.truthy(params.get("失败")):
                    raise RuntimeError("训练任务异常退出")
                points = predictions.build_points(
                    model, batch_no, period, count, metric, start_id=0)
                # 成功后把同一条批次补全，避免同一训练批次号留下两条记录。
                self._complete_batch(pending, points)
                batch = pending
                model["训练批次"] = batch_no
                model["业务周期"] = period
                model["status"] = "已就绪"
                model["pending"] = False
                if metric:
                    model["指标编码"] = predictions.metric_code_of(metric)
                model["评估误差"] = predictions.format_value(
                    0.01 + (int(hashlib.sha256(f"{batch_no}|{period}".encode()).hexdigest()[:4], 16) % 30) / 1000,
                    3)
                store.bump_version(MODULE)
                store.bump_version(PREDICTION_MODULE)
                return {"ok": True, "message": f"训练完成，已生成 {len(points)} 个预测点",
                        "batch": _batch_meta(batch)}
            except Exception as exc:  # noqa: BLE001 - 事务边界统一回滚
                store.restore(snapshot)
                last = self._last_complete(model)
                return {
                    "ok": False,
                    "message": f"重新训练失败：{exc}；已回滚本次写入，保留最近完整预测结果",
                    "lastBatch": _batch_meta(last) if last else None,
                }

    def train_and_predict(self, entry_id: int, values: list[float]) -> dict[str, Any]:
        """对历史数据执行简单移动平均或线性回归预测，并把结果留档到训练批次。"""
        entry = self.get_entry(entry_id)
        if not entry:
            return {"ok": False, "message": "模型不存在"}
        model_type = entry.get("模型类型", "moving_average")
        try:
            predict_periods = int(entry.get("预测周期", "3"))
        except Exception:
            predict_periods = 3
        if len(values) < 2:
            return {"ok": False, "message": "历史数据不足"}
        predictions_list: list[float] = []
        if model_type == "moving_average" or "moving" in str(model_type):
            try:
                window = int(str(entry.get("参数配置", "window=3")).replace("window=", ""))
            except Exception:
                window = 3
            for _ in range(predict_periods):
                pred = sum(values[-window:]) / window
                predictions_list.append(round(pred, 4))
                values = values + [pred]
        elif "linear" in str(model_type).lower() or "regression" in str(model_type).lower():
            n = len(values)
            x_mean = (n - 1) / 2
            y_mean = sum(values) / n
            num = sum((i - x_mean) * (values[i] - y_mean) for i in range(n))
            denom = sum((i - x_mean) ** 2 for i in range(n))
            slope = num / denom if denom != 0 else 0
            intercept = y_mean - slope * x_mean
            for i in range(predict_periods):
                pred = slope * (n + i) + intercept
                predictions_list.append(round(pred, 4))
        else:
            predictions_list = [sum(values) / len(values) for _ in range(predict_periods)]

        period = str(entry.get("业务周期") or self._current_period())
        batch_no = str(entry.get("训练批次") or "").strip() or self._next_batch_no(period)
        metric = self._metric_for(entry)
        precision = predictions.precision_of(metric)
        base = round(sum(values) / len(values), precision)
        points = [
            predictions.point_payload(
                model=entry, batch_no=batch_no, period=period, index=index, point_id=0,
                value=value, base=base, metric=metric)
            for index, value in enumerate(predictions_list)
        ]
        with store.lock:
            existing = next(
                (batch for batch in store.rows(PREDICTION_MODULE)
                 if batch.get("训练批次") == batch_no and batch.get("模型id") == entry.get("id")),
                None,
            )
            if existing is not None and _complete(existing):
                for index, point in enumerate(points):
                    point["id"] = int(existing.get("id", 0)) * 1000 + index + 1
                existing.update({
                    "业务周期": period,
                    "points": points,
                    "预测点数": len(points),
                    "状态": "已完成",
                    "完整": True,
                    "指标编码": predictions.metric_code_of(metric),
                    "精度位数": precision,
                    "指标口径": predictions.calibre_of(metric),
                    "完成时间": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "错误": "",
                })
            else:
                self._persist_points(entry, batch_no, period, points, status="已完成", complete=True)
            entry["训练批次"] = batch_no
            store.bump_version(PREDICTION_MODULE)
        return {"ok": True, "predictions": predictions_list, "method": model_type,
                "trainingBatch": batch_no, "metricCode": predictions.metric_code_of(metric),
                "precision": precision, "calibre": predictions.calibre_of(metric),
                "message": f"预测完成，{len(predictions_list)}个周期"}

    # ------------------------------------------------------------------
    # 分析看板：同一组参数重复运行返回同一组数（不再 random）
    # ------------------------------------------------------------------
    def run_analysis(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        try:
            count = int(params.get("样本范围", params.get("并行数量", "6")))
        except (TypeError, ValueError):
            count = 6
        count = max(1, min(count, 60))
        seed_text = json.dumps(params or {}, ensure_ascii=False, sort_keys=True)
        results: list[dict[str, Any]] = []
        for i in range(count):
            digest = hashlib.sha256(f"{seed_text}|{i}".encode("utf-8")).hexdigest()

            def unit(offset: int) -> float:
                return int(digest[offset:offset + 8], 16) / 0xFFFFFFFF

            value = round(0.62 + unit(0) * 0.40, 3)
            base = round(0.58 + unit(8) * 0.37, 3)
            results.append({
                "id": i + 1,
                "分组": f"分组{(i % 3) + 1}",
                "指标值": value,
                "对比基准": base,
                "差异": round(value - base, 3),
            })
        return results
