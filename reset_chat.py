"""
清理对话历史与长期记忆锚点（已归档的单据不受影响）

为什么需要它:
模型会把自己过去的回复当作范例，随上下文一起回灌。如果它曾经错误地回答过
"我无法发送文件"，这个错误回答就会变成范例、被反复强化。清掉对话历史可以切断这种污染。

用法:
  python3 reset_chat.py             # 备份后清空对话历史与记忆锚点（单据保留）
  python3 reset_chat.py --dry-run   # 只统计会清理多少条，不做任何修改
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from core.config import Config      # noqa: E402
from core.db import DatabaseManager  # noqa: E402


def backup(db: DatabaseManager) -> Path:
    """清空前先把对话历史与上下文游标导出留底。

    刻意绕开 db.query_documents(): 那是给模型用的查询通道，写死了
    MAX_QUERY_ROWS(200) 行上限——备份必须一条不漏，否则下面的 DELETE 一执行，
    超出上限的部分就永久没了（实测 250 条消息只导出 200 条）。
    """
    with db.get_readonly_connection() as conn:
        messages = [dict(r) for r in conn.execute("SELECT * FROM chat_messages ORDER BY id;")]
        states = [dict(r) for r in conn.execute("SELECT * FROM chat_context_state;")]

    Config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    target = Config.DATA_DIR / f"chat_backup_{datetime.now():%Y%m%d_%H%M%S}.json"
    target.write_text(
        json.dumps({"messages": messages, "context_state": states}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description="清理对话历史（单据保留）")
    parser.add_argument("--dry-run", action="store_true", help="只统计，不修改")
    args = parser.parse_args()

    db = DatabaseManager()
    message_count = db.query_documents("SELECT COUNT(*) AS c FROM chat_messages;")[0]["c"]
    doc_count = db.query_documents("SELECT COUNT(*) AS c FROM documents;")[0]["c"]

    if args.dry_run:
        print(f"[dry-run] 将清理 {message_count} 条对话消息与记忆锚点；{doc_count} 条已归档单据不受影响")
        return

    if not message_count:
        print("对话历史已经是空的，无需清理。")
        return

    backup_path = backup(db)

    # 删之前回读备份文件核对条数。这个脚本只干"备份然后清空"一件事，
    # 备份不完整就等于删数据，不能让这种失败悄悄过去。
    written = json.loads(backup_path.read_text(encoding="utf-8"))
    if len(written.get("messages", [])) != message_count:
        print(f"[!] 备份只写出 {len(written.get('messages', []))}/{message_count} 条，"
              f"已中止清理，数据库未做任何改动。")
        print(f"    备份文件（内容不完整，可自行删除）: {backup_path}")
        return

    with db.get_connection() as conn:
        conn.execute("DELETE FROM chat_messages;")
        conn.execute("DELETE FROM chat_context_state;")
        conn.commit()

    print(f"已清理 {message_count} 条对话消息与全部记忆锚点。")
    print(f"已归档的 {doc_count} 条单据保持不变，仍可正常检索。")
    print(f"清空前的备份: {backup_path}")


if __name__ == "__main__":
    main()
