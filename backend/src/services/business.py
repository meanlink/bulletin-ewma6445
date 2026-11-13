"""EWMA broadcast exception analysis; delivery is delegated to the real stack worker."""
from datetime import date, timedelta
import math
import random
import statistics


def metadata():
    return {
        "name": "播出简报异常编排", "short": "bulletin-ewma", "tagline": "先把持续小偏移识别出来，再把带口径的简报送进本地收件箱",
        "client": "模拟客户：海棠融媒播控中心，9名值班员维护3条节目播出链路。",
        "workflow": "早班下载播控日记，人工按完成率排序，把异常截图复制到值班简报，再由调度员定时整理。",
        "pains": ["小幅连续偏移容易被单日阈值漏掉", "简报缺少基准期和控制限口径", "生成结果与分发计划脱节，难以追踪投递记录"],
        "upstream": "播控日记的计划分钟、实播分钟、停播分钟与收视触达日汇总", "downstream": "播控早会与值班组本地简报收件箱；不会发送真实邮件。",
        "entities": ["播控日记", "异常简报运行", "定时分发计划"], "dataset_name": "海棠融媒_三链路120日播出日记.csv",
        "columns": [{"key": "day", "label": "播出日", "type": "string"}, {"key": "channel", "label": "播出链路", "type": "string"}, {"key": "planned_min", "label": "计划分钟", "type": "number"}, {"key": "aired_min", "label": "实播分钟", "type": "number"}, {"key": "interruption_min", "label": "停播分钟", "type": "number"}, {"key": "reach", "label": "触达估计", "type": "number"}],
        "parameters": [{"key": "channel", "label": "播出链路", "type": "select", "default": "新闻一频", "options": [{"value": "新闻一频", "label": "新闻一频"}, {"value": "文化二频", "label": "文化二频"}, {"value": "城市直播", "label": "城市直播"}]}, {"key": "metric", "label": "监测口径", "type": "select", "default": "completion", "options": [{"value": "completion", "label": "实播完成率%"}, {"value": "interruption", "label": "停播分钟"}]}, {"key": "smoothing", "label": "EWMA平滑系数λ", "type": "number", "default": 0.2, "min": 0.05, "max": 0.8}, {"key": "control_limit", "label": "控制限倍数L", "type": "number", "default": 3, "min": 2, "max": 4}],
        "workbench_title": "基准期、控制限与简报编排", "action_label": "生成异常简报", "unit": "% / 分钟", "accent": "oklch(50% 0.14 20)", "style": "broadcast-console",
        "seed_story": ["07:30 上传海棠融媒_三链路120日播出日记.csv，共360行。", "07:40 选择新闻一频，核对计划720分钟的日口径。", "07:50 固定最初30日为基准，设置λ=0.2、L=3。", "08:00 生成EWMA异常日清单，查看持续偏移与最新状态。", "08:10 切换停播分钟口径，人工核对原始播控日志。", "08:20 下载包含基准均值、标准差和参数的简报。", "08:30 为值班组建立定时分发计划，稍后在本地收件箱核对实际worker投递记录。"],
        "method": "前30日固定为基准估计μ、样本σ；之后Z_t=λX_t+(1−λ)Z_(t−1)，Z_0=μ。控制限μ±Lσ√{λ/(2−λ)[1−(1−λ)^(2t)]}；越限日进入异常清单。",
        "limitations": ["基准期需由业务确认稳定；序列自相关、节假日和排播结构会影响误报率。", "控制图识别统计偏移，不自动认定事故；投递仅进入本地收件箱，不发送真实外部消息。"],
    }


def seed_records():
    rng = random.Random(1020)
    start = date(2026, 7, 1)
    rows = []
    for channel, baseline, drift in (("新闻一频", 3.8, 8), ("文化二频", 5.2, 5.5), ("城市直播", 7.4, 12)):
        for index in range(120):
            planned = 720.0 if channel != "城市直播" else 360.0
            downtime = max(0.2, rng.gauss(baseline, 1.15))
            if 77 <= index <= 85:
                downtime += drift
            if index == 112:
                downtime += 15
            downtime = round(downtime, 2)
            rows.append({"day": str(start + timedelta(days=index)), "channel": channel, "planned_min": planned, "aired_min": round(planned-downtime, 2), "interruption_min": downtime, "reach": round(rng.uniform(140000, 210000))})
    return rows


def _parameter(value, name, low, high):
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name}必须为数值") from None
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{name}须介于{low}至{high}")
    return value


def analyze(records, params):
    if not isinstance(records, list) or not records:
        raise ValueError("请先导入播控日记")
    channel = str(params.get("channel", "新闻一频"))
    metric = params.get("metric", "completion")
    if metric not in ("completion", "interruption"):
        raise ValueError("未知监测口径")
    smoothing = _parameter(params.get("smoothing", 0.2), "平滑系数", 0.05, 0.8)
    control_limit = _parameter(params.get("control_limit", 3), "控制限倍数", 2, 4)
    parsed, days = [], set()
    consistency_warnings = 0
    for row in records:
        if str(row.get("channel")) != channel:
            continue
        try:
            day = date.fromisoformat(str(row["day"]))
            planned, aired, interruption = [float(row[key]) for key in ("planned_min", "aired_min", "interruption_min")]
        except (TypeError, ValueError, KeyError):
            raise ValueError("播出日须为ISO日期，计划、实播与停播分钟须为数值") from None
        if not all(math.isfinite(value) for value in (planned, aired, interruption)) or not 0 < planned <= 1440 or not 0 <= aired <= planned or not 0 <= interruption <= planned:
            raise ValueError("日计划分钟须大于0且不超过1440，实播与停播分钟须在0至计划分钟之间")
        if day in days:
            raise ValueError("同一链路同日存在重复日记，请先去重")
        days.add(day)
        if abs(planned - aired - interruption) > 0.05:
            consistency_warnings += 1
        value = aired / planned * 100 if metric == "completion" else interruption
        parsed.append((day, value))
    parsed.sort()
    if len(parsed) < 60:
        raise ValueError("所选链路至少需要60日，前30日作为固定基准")
    if any((parsed[index][0] - parsed[index-1][0]).days != 1 for index in range(1, len(parsed))):
        raise ValueError("EWMA要求连续日历，缺日请补充原始播控日记")
    baseline_values = [value for _, value in parsed[:30]]
    mean = statistics.mean(baseline_values)
    sigma = statistics.stdev(baseline_values)
    if sigma < 1e-10:
        raise ValueError("基准期无波动，无法估计控制限；请换用已确认稳定且有波动的基准数据")
    z = mean
    output = []
    for index, (day, value) in enumerate(parsed[30:], 1):
        z = smoothing * value + (1 - smoothing) * z
        half_width = control_limit * sigma * math.sqrt(smoothing / (2-smoothing) * (1-(1-smoothing)**(2*index)))
        lower, upper = mean-half_width, mean+half_width
        anomaly = z < lower or z > upper
        output.append({"day": str(day), "actual": round(value, 4), "ewma": round(z, 4), "lower": round(lower, 4), "upper": round(upper, 4), "status": "越下限" if z < lower else "越上限" if z > upper else "受控", "anomaly": anomaly})
    anomalies = [row for row in output if row["anomaly"]]
    unit = "%" if metric == "completion" else "分钟"
    latest = output[-1]
    warnings = []
    if consistency_warnings:
        warnings.append(f"有{consistency_warnings}行计划≠实播+停播，简报前需核对源日记口径。")
    if len(anomalies) > len(output) * 0.25:
        warnings.append("越限日超过监测期四分之一，需复核基准期或是否发生结构性变化。")
    table_rows = [{key: value for key, value in row.items() if key != "anomaly"} for row in (anomalies[-25:] if anomalies else output[-14:])]
    return {
        "summary": f"{channel}以{parsed[0][0]}至{parsed[29][0]}为固定基准；后续{len(output)}日中{len(anomalies)}日EWMA越限，最新日{latest['day']}为{latest['status']}。",
        "metrics": [{"label": "基准均值", "value": round(mean, 4), "unit": unit}, {"label": "基准标准差", "value": round(sigma, 4), "unit": unit}, {"label": "越限日数", "value": len(anomalies), "unit": "日"}, {"label": "最新EWMA", "value": latest["ewma"], "unit": unit}],
        "table": {"columns": [{"key": "day", "label": "播出日"}, {"key": "actual", "label": f"实测({unit})"}, {"key": "ewma", "label": "EWMA"}, {"key": "lower", "label": "下控制限"}, {"key": "upper", "label": "上控制限"}, {"key": "status", "label": "简报标记"}], "rows": table_rows},
        "chart": {"type": "line", "labels": [row["day"] for row in output], "series": [{"name": "EWMA", "values": [row["ewma"] for row in output]}, {"name": "下控制限", "values": [row["lower"] for row in output]}, {"name": "上控制限", "values": [row["upper"] for row in output]}]},
        "findings": [f"λ={smoothing:g}，L={control_limit:g}；基准固定30日，不使用未来监测值重新估计均值与标准差。", f"{'最近越限日为'+anomalies[-1]['day']+'，状态'+anomalies[-1]['status'] if anomalies else '本次未检测到越限日；仍应留存基准和参数'}。", "异常清单只展示最近25个越限日；无越限时展示最近14日，完整序列保留在图表数据。", "在分发模块选择本运行结果并设置计划；worker按实际时间投递至本地收件箱，分析函数不会发送消息。"],
        "method": metadata()["method"], "warnings": warnings,
    }
