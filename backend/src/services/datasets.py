from fastapi import HTTPException
from sqlalchemy import select
from src.models import Dataset
from src.services.business import metadata
from src.services.ingest import initial_ingest, merge_ingest, summarize, public_records
from src.utils.serialization import checksum
import time


def create(db, data):
    name = data.name.strip()
    if len(name) < 2:
        raise HTTPException(422, '批次名称去除空格后至少需要两个字符')
    if db.scalar(select(Dataset).where(Dataset.name == name)):
        raise HTTPException(409, '该数据批次名已存在，请修改名称')
    source = (data.source or 'manual.json').strip() or 'manual.json'
    valid, rejected, ingest_log = initial_ingest(data.records, source)
    if not valid:
        raise HTTPException(422, '没有可接入的有效记录，请先修正坏记录')
    d = Dataset(
        name=name, source=source, records=valid, checksum=checksum(public_records(valid)),
        created_at=time.time(), ingest_log=ingest_log,
        rejected_records=rejected, revision=1,
    )
    db.add(d)
    db.flush()
    return d


def append(db, dataset_id, data):
    """向已有批次补入日记：晚到记录按链路+播出日合并，同日以较新修订为准。"""
    d = db.get(Dataset, dataset_id)
    if not d:
        raise HTTPException(404, '数据批次不存在')
    if not data.records:
        raise HTTPException(422, '补报至少需要一条记录')
    source = (data.source or 'supplement.json').strip() or 'supplement.json'
    merged, rejected, ingest_log, stats = merge_ingest(
        d.records or [], d.rejected_records or [], d.ingest_log or [],
        data.records, source,
    )
    d.records = merged
    d.rejected_records = rejected
    d.ingest_log = ingest_log
    d.revision = stats['seq']
    d.checksum = checksum(public_records(merged))
    db.flush()
    return d, stats


def public_dataset_records(d):
    """对外展示/参与计算的业务记录，不含内部修订留痕字段。"""
    return public_records(d.records or [])


def intake_summary(d):
    return summarize(d.records or [], d.rejected_records or [], d.ingest_log or [])
