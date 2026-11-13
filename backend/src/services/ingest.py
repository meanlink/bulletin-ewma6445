"""播控日记补报与更正的接入合并逻辑（纯标准库，便于脱离框架测试）。

合并键为 (播出链路 channel, 播出日 day)：
- 晚到记录按链路与播出日并入既有批次；
- 同一键同日多版时，以接收批次序号较大（较新修订）为准；
- 重复内容或旧修订不改变既有结果，计入 ignored；
- 校验不通过的坏记录单独列明原因，计入 rejected，绝不混入有效记录。
"""
from datetime import date

# 与 business.metadata 的列定义保持一致；顺序即校验提示顺序
COLUMNS = (
    ("day", "播出日", "string"),
    ("channel", "播出链路", "string"),
    ("planned_min", "计划分钟", "number"),
    ("aired_min", "实播分钟", "number"),
    ("interruption_min", "停播分钟", "number"),
    ("reach", "触达估计", "number"),
)
NUMBER_KEYS = tuple(key for key, _, kind in COLUMNS if kind == "number")
DAY_KEY, CHANNEL_KEY = "day", "channel"
# 可选字段：补报行可声明它替换的是哪一版（批次序号）。当前版本已更新时，
# 针对更旧版本的迟到更正只能被忽略，不能回滚已生效的新修订。
REVISION_KEY = "replaces_revision"
INTERNAL_KEYS = ("_revision",)
REASON_DUPLICATE = "与批次内同链路同日记录重复"
REASON_CONFLICT = "同一次补报中同链路同日出现两个不同版本，无法确定以哪版为准"
REASON_OLD = "旧修订，批次内已存在较新或同等版本，未改变结果"


def public_records(records):
    """剥离内部留痕字段，返回可对外展示与参与计算的业务记录。"""
    return [{k: v for k, v in row.items() if k not in INTERNAL_KEYS} for row in records]


def validate_row(row):
    """校验单行；通过返回规整后的 dict，否则返回 (None, 原因)。"""
    if not isinstance(row, dict):
        return None, "记录必须为对象"
    for key, label, kind in COLUMNS:
        value = row.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            return None, f"缺少字段 {label}"
        if kind == "number":
            if isinstance(value, bool):
                return None, f"{label} 必须为有限数值"
            try:
                number = float(value)
            except (TypeError, ValueError, OverflowError):
                return None, f"{label} 必须为有限数值"
            if number != number or number in (float("inf"), float("-inf")):
                return None, f"{label} 必须为有限数值"
            row[key] = number
    day_text = str(row[DAY_KEY]).strip()
    try:
        day = date.fromisoformat(day_text)
    except ValueError:
        return None, "播出日须为 YYYY-MM-DD 格式的 ISO 日期"
    channel = str(row[CHANNEL_KEY]).strip()
    if not channel:
        return None, "播出链路不能为空"
    planned, aired, interruption = row["planned_min"], row["aired_min"], row["interruption_min"]
    if not 0 < planned <= 1440:
        return None, "日计划分钟须大于0且不超过1440"
    if not 0 <= aired <= planned:
        return None, "实播分钟须在0至计划分钟之间"
    if not 0 <= interruption <= planned:
        return None, "停播分钟须在0至计划分钟之间"
    clean = {key: row[key] for key, _, _ in COLUMNS}
    clean[DAY_KEY] = str(day)
    clean[CHANNEL_KEY] = channel
    # 可选的旧版声明：仅接受正整数，非法值直接忽略该字段，不影响行校验
    declared = row.get(REVISION_KEY)
    if isinstance(declared, bool):
        declared = None
    try:
        if declared is not None and int(declared) >= 1:
            clean[REVISION_KEY] = int(declared)
    except (TypeError, ValueError):
        pass
    return clean, None


def _key_of(row):
    return row[CHANNEL_KEY], row[DAY_KEY]


def _business_view(row):
    """仅六个业务列，用于判定两版内容是否相同（忽略版本声明与内部留痕）。"""
    return {key: row[key] for key, _, _ in COLUMNS}


def initial_ingest(incoming, source):
    """首次接入：校验全部记录，构造初始批次的有效记录、拒收清单与批次日志。

    返回的有效记录带内部 _revision=1，持久化时保留，对外用 public_records 剥离。
    """
    valid, rejected, seen = [], [], {}
    for index, raw in enumerate(incoming):
        clean, reason = validate_row(dict(raw) if isinstance(raw, dict) else raw)
        if reason:
            rejected.append({"index": index + 1, "reason": reason, "record": raw})
            continue
        key = _key_of(clean)
        if key in seen:
            # 首次接入保持严格语义：同批同键冲突直接拒收，绝不静默覆盖
            same = _business_view(seen[key]) == _business_view(clean)
            rejected.append({"index": index + 1,
                             "reason": REASON_DUPLICATE if same else REASON_CONFLICT,
                             "record": public_records([clean])[0]})
            continue
        tagged = {**clean, "_revision": 1}
        seen[key] = tagged
        valid.append(tagged)
    log = [{
        "seq": 1, "source": source, "received": len(incoming),
        "accepted": len(valid), "ignored": 0, "rejected": len(rejected),
    }]
    return valid, rejected, log


def merge_ingest(records, rejected_log, ingest_log, incoming, source):
    """向既有批次补入日记，返回 (新有效记录, 新拒收清单, 新批次日志, 本批统计)。

    records/ingest_log 为只读输入；rejected_log 会追加新拒收项。
    修订先后只看接收批次序号：seq 越大代表越新的修订。
    行内可选 replaces_revision 声明它针对的旧版；当前版本已更新时该迟到旧版被忽略。
    """
    next_seq = (ingest_log[-1]["seq"] + 1) if ingest_log else 1
    current = {_key_of(row): row for row in records}
    current_rev = {_key_of(row): row.get("_revision", 1) for row in records}
    # 同一次补报内部先定版：同键只接受第一个，其余拒收（重复/冲突原因区分）
    staged, staged_seen = [], {}
    new_rejected, accepted, ignored = [], 0, 0
    for index, raw in enumerate(incoming):
        clean, reason = validate_row(dict(raw) if isinstance(raw, dict) else raw)
        if reason:
            new_rejected.append({"index": index + 1, "reason": reason, "record": raw})
            continue
        key = _key_of(clean)
        if key in staged_seen:
            same = _business_view(staged_seen[key]) == _business_view(clean)
            new_rejected.append({"index": index + 1,
                                 "reason": REASON_DUPLICATE if same else REASON_CONFLICT,
                                 "record": public_records([clean])[0]})
            continue
        staged_seen[key] = clean
        staged.append(clean)
    for clean in staged:
        key = _key_of(clean)
        existing = current.get(key)
        declared = clean.pop(REVISION_KEY, None)
        business = _business_view(clean)
        if existing is None:
            # 晚到的新播出日，按链路/播出日并入；声明针对旧版而该键不存在时仍作为新记录接入
            current[key] = {**business, "_revision": next_seq}
            current_rev[key] = next_seq
            accepted += 1
            continue
        if _business_view(existing) == business:
            # 业务内容完全一致：重复补报，不改变结果，也不推进版本号
            ignored += 1
            continue
        if declared is not None and declared < current_rev[key]:
            # 针对旧版本的迟到更正，当前已有更新修订：旧修订不得回滚结果。
            # 它不是坏记录（校验合法），只计入忽略并留痕，保证 接收=并入+忽略+拒收 恒等。
            new_rejected.append({"index": None, "reason": REASON_OLD,
                                 "record": business, "kind": "ignored"})
            ignored += 1
            continue
        # 同键不同内容：较新批次（本批 seq 更大）覆盖旧修订，并推进版本号
        current[key] = {**business, "_revision": next_seq}
        current_rev[key] = next_seq
        accepted += 1
    # 稳定排序，保证存储可复现：先链路后播出日；_revision 随存储保留用于跨批次判版
    merged = sorted(current.values(), key=lambda r: (r[CHANNEL_KEY], r[DAY_KEY]))
    rejected_entries = [item for item in new_rejected if item.get("kind") != "ignored"]
    ignored_entries = [item for item in new_rejected if item.get("kind") == "ignored"]
    stats = {"received": len(incoming), "accepted": accepted, "ignored": ignored,
             "rejected": len(rejected_entries)}
    entry = {"seq": next_seq, "source": source, **stats}
    logged = [{"batch_seq": next_seq, **item} for item in new_rejected]
    return merged, rejected_log + logged, ingest_log + [entry], {**stats, "seq": next_seq,
                                                                 "ignored_entries": ignored_entries}


def summarize(records, rejected_log, ingest_log):
    """页面核对用汇总：接收（累计/本批）、忽略、拒收与当前有效记录数。"""
    received_total = sum(entry["received"] for entry in ingest_log)
    ignored_total = sum(entry["ignored"] for entry in ingest_log)
    accepted_total = sum(entry["accepted"] for entry in ingest_log)
    rejected_total = sum(1 for item in rejected_log if item.get("kind") != "ignored")
    return {
        "current_rows": len(records),
        "received_total": received_total,
        "accepted_total": accepted_total,
        "ignored_total": ignored_total,
        "rejected_total": rejected_total,
        "batches": len(ingest_log),
        # 恒等式：累计接收 = 累计并入 + 累计忽略 + 累计拒收
        "balanced": received_total == accepted_total + ignored_total + rejected_total,
    }
