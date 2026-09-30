"""
SQLite 统一存储与混合查询管理模块 (支持 JSON1 动态扩展)

读写通道严格分离:
- 写入 (建表/入库/对话留底) 走 get_connection()，可读可写；
- 查询走 get_readonly_connection()，文件级 mode=ro + SQLite authorizer 白名单双重限制，
  从根上杜绝模型生成 SQL 带来的写库/改表/挂载外部库风险，不依赖关键字黑名单。
"""
import sqlite3
import json
import difflib
from pathlib import Path
from typing import Dict, Any, List, Optional

from .config import Config

# 只读查询允许的 SQLite 动作白名单: 仅取数、读列、调用函数(SUM/COUNT/json_extract...)
READONLY_ALLOWED_ACTIONS = frozenset({
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
})


class DatabaseManager:
    def __init__(self, db_path: Optional[Path] = None):
        # 路径只认 Config.DB_PATH 一个出处。这里原先自己又定义了一份 DEFAULT_DB_PATH，
        # 结果"改 config 里的路径不生效"——正是 config.py 开头抱怨过的那类坑。
        self.db_path = db_path or Config.DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    def get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        return conn

    @staticmethod
    def _readonly_authorizer(action: int, arg1, arg2, db_name, trigger_name) -> int:
        if action in READONLY_ALLOWED_ACTIONS:
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    def get_readonly_connection(self) -> sqlite3.Connection:
        """只读连接 (mode=ro + authorizer): 物理上无法执行任何写操作"""
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        conn.set_authorizer(self._readonly_authorizer)
        return conn

    def init_db(self):
        with self.get_connection() as conn:
            # 1. 统一文档凭证混合表 (通用列 + 动态 JSON 列)
            conn.execute("""
            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                doc_type TEXT NOT NULL,         -- 'invoice', 'contract', 'delivery', 'receipt', 'other'
                title TEXT NOT NULL,            -- 文档标题/摘要说明
                main_amount REAL DEFAULT 0.0,   -- 核心金额 (方便 SUM/AVG 聚合计算)
                main_date TEXT,                 -- 核心发生日期 (YYYY-MM-DD)
                main_entity TEXT,               -- 核心业务主体 (开票方/合同对方/供货商)
                file_path TEXT,                 -- 本地原件相对路径 (归档留底)
                extra_json JSON,                -- 动态扩展字段 (异构属性)
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_doc_type ON documents(doc_type);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_main_date ON documents(main_date);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_main_entity ON documents(main_entity);")

            # 2. 全量物理对话流水表 (永久留底，防丢失；单用户场景无需会话列)
            conn.execute("""
            CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                role TEXT NOT NULL,             -- 'user', 'assistant', 'system', 'tool'
                content TEXT,
                tool_calls_json TEXT,           -- 工具调用参数 (如发生)
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """)

            # 3. 上下文状态快照表 (单用户单行)：只留事实锚点，不存对话摘要
            conn.execute("""
            CREATE TABLE IF NOT EXISTS chat_context_state (
                session_id TEXT PRIMARY KEY,
                fact_anchors_json JSON,         -- 结构化关键事实锚点 (用户偏好、已确定数字、未完事项)
                compressed_up_to_id INTEGER,    -- 已移出上下文的最后一条消息 ID
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """)
            conn.commit()

    def insert_document(self, doc_type: str, title: str, main_amount: float,
                        main_date: str, main_entity: str, file_path: str = "",
                        extra_data: Optional[Dict[str, Any]] = None) -> int:
        extra_json = json.dumps(extra_data or {}, ensure_ascii=False)
        with self.get_connection() as conn:
            cursor = conn.execute("""
            INSERT INTO documents (doc_type, title, main_amount, main_date, main_entity, file_path, extra_json)
            VALUES (?, ?, ?, ?, ?, ?, ?);
            """, (doc_type, title, main_amount, main_date, main_entity, file_path, extra_json))
            conn.commit()
            return cursor.lastrowid

    # ---------------------------------------------------------------- 写入通道
    # 只给两个叫得出名字的操作，**不接受任何模型生成的 SQL**——模型能改数据，
    # 但不能自己编语句去改。只读那条原则（mode=ro + authorizer 白名单）原样不动。
    WRITABLE_COLUMNS = frozenset({"doc_type", "title", "main_amount",
                                  "main_date", "main_entity"})

    def delete_document(self, doc_id: int) -> int:
        """按编号删一条单据，返回删掉的行数（0 表示编号不存在）"""
        with self.get_connection() as conn:
            cur = conn.execute("DELETE FROM documents WHERE id = ?;", (int(doc_id),))
            conn.commit()
            return cur.rowcount

    def update_document(self, doc_id: int, updates: Dict[str, Any]) -> int:
        """按编号改若干列，返回改动的行数。

        只认 documents 表的通用列；`extra_json` 里那些模型自己归纳的字段不在此列
        ——改它不如让模型重新归档一次，免得改出前后对不上的记录。
        """
        updates = {k: v for k, v in (updates or {}).items() if k in self.WRITABLE_COLUMNS}
        if not updates:
            return 0
        sets = ", ".join(f"{col} = ?" for col in updates)   # 列名来自白名单，不是拼用户输入
        with self.get_connection() as conn:
            cur = conn.execute(f"UPDATE documents SET {sets} WHERE id = ?;",
                               (*updates.values(), int(doc_id)))
            conn.commit()
            return cur.rowcount

    def query_documents(self, sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
        """只读 SQL 查询 (支持 SQLite JSON1 穿透)，结果行数上限 MAX_QUERY_ROWS"""
        with self.get_readonly_connection() as conn:
            cursor = conn.execute(sql, params)
            rows = cursor.fetchmany(Config.MAX_QUERY_ROWS)
            results = []
            for r in rows:
                item = dict(r)
                if item.get("extra_json"):
                    try:
                        item["extra_json"] = json.loads(item["extra_json"])
                    except (TypeError, ValueError):
                        pass
                results.append(item)
            return results

    def find_similar_documents(self, main_entity: str, main_amount: float, main_date: str,
                               exclude_id: Optional[int] = None,
                               day_window: int = 3, amount_tolerance: float = 0.05,
                               limit: int = 3) -> List[Dict[str, Any]]:
        """
        确定性重复凭证检测: 主体相似 + 日期相近 + 金额相同或接近。

        只用于"提醒用户"，绝不自动合并 —— 同一笔消费可能同时有团购截图、支付截图和
        正规发票，三者金额与主体名往往并不一致；规则匹配不到时宁可返回空，
        也不靠模型猜测去合并（误合并比不合并危害更大）。
        """
        if not main_date:
            return []
        try:
            rows = self.query_documents(
                "SELECT id, doc_type, title, main_amount, main_date, main_entity FROM documents"
                " WHERE main_amount > 0 AND julianday(main_date) IS NOT NULL"
                "   AND ABS(julianday(main_date) - julianday(?)) <= ?"
                " ORDER BY id DESC LIMIT 30;",
                (main_date, day_window),
            )
        except sqlite3.DatabaseError:
            return []

        matched = []
        for row in rows:
            if exclude_id is not None and row["id"] == exclude_id:
                continue
            if not _amounts_close(row["main_amount"], main_amount, amount_tolerance):
                continue
            if not _entities_similar(row["main_entity"], main_entity):
                continue
            matched.append(row)
            if len(matched) >= limit:
                break
        return matched


def _amounts_close(a: Any, b: Any, tolerance: float) -> bool:
    try:
        a, b = float(a), float(b)
    except (TypeError, ValueError):
        return False
    if a <= 0 or b <= 0:
        return False
    return abs(a - b) <= max(0.01, b * tolerance)


def _entities_similar(a: Optional[str], b: Optional[str]) -> bool:
    """主体名相似判断: 互相包含，或字符序列相似度达标"""
    a = (a or "").strip()
    b = (b or "").strip()
    if not a or not b or a == "未知主体" or b == "未知主体":
        return False
    if a in b or b in a:
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.6
