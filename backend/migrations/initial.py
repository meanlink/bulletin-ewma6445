from sqlalchemy import inspect, text
from src.config.database import Base, engine
# 补报/更正在 datasets 上新增的列；老库直接 ALTER 补齐，新库由 create_all 建立
_ADDED_COLUMNS = {
    'datasets': {
        'ingest_log': 'JSON',
        'rejected_records': 'JSON',
        'revision': 'INTEGER',
    },
}


def migrate():
    Base.metadata.create_all(engine)
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table, columns in _ADDED_COLUMNS.items():
            if table not in existing_tables:
                continue
            present = {column['name'] for column in inspector.get_columns(table)}
            for name, ddl_type in columns.items():
                if name not in present:
                    conn.execute(text(f'ALTER TABLE {table} ADD COLUMN {name} {ddl_type}'))


if __name__ == '__main__':
    migrate()
