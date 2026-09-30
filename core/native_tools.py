"""
原生轻量 Tool Calling 工具定义与执行器 (零外部重型依赖)

安全模型:
1. 查询固定跑在只读连接上 (mode=ro + authorizer 白名单，见 db.get_readonly_connection)，
   模型即使被单据里的文字诱导生成 DROP/ATTACH/PRAGMA 写入语句，也会在执行层被拒绝。
2. 工具结果会剥离 file_path: 服务器上的归档路径对用户毫无意义，暴露给模型只会让它
   把路径念给用户听，所以只回传 has_file 这一位信息。
3. 写操作只有"按编号改"和"按编号删"两个窄接口，语句由程序写死——模型能改数据，
   但给不了 SQL，所以上面第 1 条依然成立。
"""
import sqlite3
from typing import Dict, Any, List

from .config import Config
from .db import DatabaseManager

READONLY_REJECT_MESSAGE = "安全拦截: 该工具仅支持 SELECT 只读查询"


class NativeToolRegistry:
    def __init__(self, db: DatabaseManager):
        self.db = db

    def get_tool_definitions(self) -> List[Dict[str, Any]]:
        """返回符合 OpenAI/DeepSeek 标准的 Tools Schema 列表"""
        return [
            {
                "type": "function",
                "function": {
                    "name": "query_documents_by_sql",
                    "description": (
                        "执行只读 SQL 查询以统计或检索单据数据。"
                        "documents 表可查的列: id, doc_type, title, main_amount, main_date, main_entity；"
                        "支持 SUM / COUNT / GROUP BY。"
                        "单据上的其他信息（单号、甲乙方、消费明细、税率构成……）都在 extra_json 里，"
                        "用 json_extract(extra_json,'$.type_fields.<键>') 取——"
                        "**键名是中文、随单据而异，没有固定清单**，"
                        "不确定有哪些键就先把整个对象取出来看。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "sql": {
                                "type": "string",
                                "description": "要执行的只读 SQLite 查询语句，例如: SELECT title, main_amount, main_date FROM documents WHERE main_entity LIKE '%海底捞%'"
                            }
                        },
                        "required": ["sql"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "find_documents",
                    "description": "按关键字、单据类型或年月检索已归档的单据，返回编号、标题、金额、日期、对方主体。用户想找某张单据时先用它定位。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "keyword": {"type": "string", "description": "在标题或对方主体中模糊匹配的关键字，如 '晨光' / '滴滴' / '办公用品'"},
                            "doc_type": {"type": "string", "description": "单据类型过滤，如 'invoice'，不限可留空"},
                            "year_month": {"type": "string", "description": "按月份过滤，格式 'YYYY-MM'，如 '2026-08'"},
                            "limit": {"type": "integer", "description": "最多返回条数，默认 10"}
                        }
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "get_spending_summary",
                    "description": "快速按月度或单据类型统计金额合计",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "year_month": {
                                "type": "string",
                                "description": "指定统计年月，如 '2026-08'，若统计全部可不传"
                            },
                            "doc_type": {
                                "type": "string",
                                "description": "指定单据类型，如 'invoice'，不限可留空"
                            }
                        }
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "delete_document",
                    "description": (
                        "删除一条已归档的单据。**动手前必须先跟用户确认删的是哪一张**；"
                        "他只给了模糊指代（'那张'）时，先用 find_documents 列出来给他看。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "integer", "description": "要删除的单据编号"}
                        },
                        "required": ["id"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "update_document",
                    "description": (
                        "改一条已归档单据的基本信息。能改的列: title / main_amount / "
                        "main_date / main_entity / doc_type。**改之前先跟用户确认改成什么。**"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "integer", "description": "单据编号"},
                            "field": {
                                "type": "string",
                                "description": "要改的字段",
                                # 可改的列是个封闭集合，直接写进 schema 让模型选，
                                # 比在描述里列一遍、再由程序报错兜底更省一轮
                                "enum": ["title", "main_amount", "main_date",
                                         "main_entity", "doc_type"],
                            },
                            "value": {"type": "string", "description": "新的值"}
                        },
                        "required": ["id", "field", "value"]
                    }
                }
            }
        ]

    def execute_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """执行本地工具，所有数据库访问都被限制为只读"""
        if name == "query_documents_by_sql":
            sql = (arguments.get("sql") or "").strip()
            if not sql:
                return {"status": "error", "message": "缺少 sql 参数"}
            try:
                results = self.db.query_documents(sql)
            except sqlite3.DatabaseError as e:
                if self._is_readonly_rejection(str(e)):
                    return {"status": "error", "message": READONLY_REJECT_MESSAGE}
                return {"status": "error", "message": f"SQL 执行错误: {str(e)}"}
            return self._cap_rows(results)

        if name == "find_documents":
            return self._find_documents(arguments)

        if name == "get_spending_summary":
            return self._spending_summary(arguments)

        if name == "delete_document":
            return self._delete_document(arguments)

        if name == "update_document":
            return self._update_document(arguments)

        return {"status": "error", "message": f"未注册的工具: {name}"}

    # ------------------------------------------------------------ 写操作
    # 全项目仅有的两个写操作。窄接口：语句程序写死，模型只能给参数（见 db.py）。
    def _delete_document(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        try:
            doc_id = int(arguments.get("id"))
        except (TypeError, ValueError):
            return {"status": "error", "message": "id 得是数字"}
        found = self.db.query_documents(
            "SELECT title, main_amount, main_date, main_entity FROM documents WHERE id = ?",
            (doc_id,))
        if not found:
            return {"status": "error", "message": f"没有编号为 {doc_id} 的单据"}
        if not self.db.delete_document(doc_id):
            return {"status": "error", "message": "删除没有生效"}
        d = found[0]
        return {"status": "success", "deleted": True,
                "message": f"已删除「{d['title']}」¥{d['main_amount']:.2f} {d['main_date']}"}

    def _update_document(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        try:
            doc_id = int(arguments.get("id"))
        except (TypeError, ValueError):
            return {"status": "error", "message": "id 得是数字"}
        field = str(arguments.get("field") or "").strip()
        value: Any = arguments.get("value")
        if field not in DatabaseManager.WRITABLE_COLUMNS:
            allowed = "、".join(sorted(DatabaseManager.WRITABLE_COLUMNS))
            return {"status": "error", "message": f"不能改 {field}；能改的只有 {allowed}"}
        if field == "main_amount":
            try:
                value = float(value)
            except (TypeError, ValueError):
                return {"status": "error", "message": "金额得是数字"}
        if not self.db.update_document(doc_id, {field: value}):
            return {"status": "error",
                    "message": f"没有编号为 {doc_id} 的单据（或新值和原来一样）"}
        return {"status": "success", "updated": True, "message": f"已把 {field} 改成 {value}"}

    # ------------------------------------------------------------ 具体工具实现
    def _find_documents(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        sql = ("SELECT id, doc_type, title, main_amount, main_date, main_entity, file_path"
               " FROM documents WHERE 1=1")
        params: List[Any] = []

        keyword = (arguments.get("keyword") or "").strip()
        if keyword:
            sql += " AND (title LIKE ? OR main_entity LIKE ?)"
            params += [f"%{keyword}%", f"%{keyword}%"]

        doc_type = (arguments.get("doc_type") or "").strip()
        if doc_type:
            sql += " AND doc_type = ?"
            params.append(doc_type)

        year_month = (arguments.get("year_month") or "").strip()
        if year_month:
            sql += " AND main_date LIKE ?"
            params.append(f"{year_month}%")

        try:
            limit = int(arguments.get("limit") or 10)
        except (TypeError, ValueError):
            limit = 10
        sql += " ORDER BY main_date DESC, id DESC LIMIT ?"
        params.append(max(1, min(limit, 50)))

        try:
            rows = self.db.query_documents(sql, tuple(params))
        except sqlite3.DatabaseError as e:
            return {"status": "error", "message": f"检索失败: {str(e)}"}

        documents = [
            {
                "id": r["id"],
                "doc_type": r["doc_type"],
                "title": r["title"],
                "main_amount": r["main_amount"],
                "main_date": r["main_date"],
                "main_entity": r["main_entity"],
                "has_file": bool(r.get("file_path")),
            }
            for r in rows
        ]
        return {"status": "success", "count": len(documents), "documents": documents}

    def _spending_summary(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        year_month = arguments.get("year_month")
        doc_type = arguments.get("doc_type")
        sql = "SELECT COUNT(*) as count, SUM(main_amount) as total_amount FROM documents WHERE 1=1"
        params: List[Any] = []
        if year_month:
            sql += " AND main_date LIKE ?"
            params.append(f"{year_month}%")
        if doc_type:
            sql += " AND doc_type = ?"
            params.append(doc_type)
        try:
            results = self.db.query_documents(sql, tuple(params))
        except sqlite3.DatabaseError as e:
            return {"status": "error", "message": f"SQL 执行错误: {str(e)}"}
        summary = results[0] if results else {"count": 0, "total_amount": 0.0}
        return {
            "status": "success",
            "total_amount": summary.get("total_amount") or 0.0,
            "count": summary.get("count") or 0
        }

    # ------------------------------------------------------------ 安全辅助
    @staticmethod
    def _cap_rows(results: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        行数上限: 工具结果会被回灌进上下文，几十上百行原始数据会挤占上下文与成本，
        超出部分只回报行数并提示改用聚合，避免"查一次就撑爆上下文"。

        同时剥离 file_path: 内部归档路径对用户毫无意义，只保留 has_file 一位。
        """
        limit = Config.MAX_TOOL_ROWS_IN_CONTEXT
        cleaned = []
        for row in results:
            item = {k: v for k, v in row.items() if k != "file_path"}
            if "file_path" in row:
                item["has_file"] = bool(row.get("file_path"))
            cleaned.append(item)

        if len(cleaned) <= limit:
            return {"status": "success", "count": len(cleaned), "data": cleaned}
        return {
            "status": "success",
            "count": len(cleaned),
            "data": cleaned[:limit],
            "truncated": True,
            "note": f"结果共 {len(cleaned)} 行，仅回传前 {limit} 行；如需更多请改用聚合统计或加 LIMIT",
        }

    @staticmethod
    def _is_readonly_rejection(err: str) -> bool:
        lowered = err.lower()
        return any(k in lowered for k in ("not authorized", "readonly", "read-only"))
