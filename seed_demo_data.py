"""
一键灌入演示数据 (样例单据原件 + 归档记录)，用于首次体验与演示。

用法:
  python3 seed_demo_data.py          # 库中还没有单据时灌入演示数据
  python3 seed_demo_data.py --reset  # 先清空 documents 表再灌入
  python3 seed_demo_data.py --force  # 忽略"已有数据"提示，直接追加

灌入后可以直接提问，例如:
  "我 8 月一共花了多少钱？"
  "晨光那张发票是多少钱？"
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "samples"))

from core.db import DatabaseManager          # noqa: E402
from core.media_reader import MediaReader    # noqa: E402
import make_samples                          # noqa: E402

SAMPLE_DIR = ROOT / "samples"

# 演示单据: source 为 samples/ 下的样例原件，其余字段刻意与真实识别结果同构——
# 固定列只有 doc_type / title / main_amount / main_date / main_entity，
# 其余（单号、甲乙方、消费明细……）一律是 type_fields，键是中文、由模型按单据内容定。
# 这里若图省事写成另一套结构，演示数据就会和真实入库的数据对不上，
# 提问时"合同甲方是谁"能查、而灌进来的样例查不到，反而更难排查。
DEMO_DOCS = [
    {
        "source": "sample_invoice.png",
        "doc_type": "invoice",
        "title": "办公用品采购-增值税电子普通发票",
        "main_amount": 1130.00,
        "main_date": "2026-08-18",
        "main_entity": "上海晨光办公用品有限公司",
        "type_fields": {"发票号码": "02514789", "不含税": "1000.00",
                        "税额": "130.00", "税率": "13%"},
    },
    {
        "source": "sample_order_screenshot.png",
        "doc_type": "order",
        "title": "罗技 MX Keys 无线键盘",
        "main_amount": 399.00,
        "main_date": "2026-08-22",
        "main_entity": "罗技旗舰店",
        "type_fields": {"订单号": "2088123456789012", "下单时间": "21:03"},
    },
    {
        "source": "sample_receipt.png",
        "doc_type": "receipt",
        "title": "餐饮消费小票",
        "main_amount": 88.50,
        "main_date": "2026-08-15",
        "main_entity": "某某餐饮有限公司",
        "type_fields": {"单号": "NO.20260815-0371",
                        "商品明细": "宫保鸡丁 58.00、米饭 6.00、可乐 24.50",
                        "消费时间": "12:41"},
    },
    {
        "source": "sample_delivery_note.png",
        "doc_type": "delivery",
        "title": "材料送货单-螺纹钢 HRB400",
        "main_amount": 51250.00,
        "main_date": "2026-08-12",
        "main_entity": "某某建材有限公司",
        "type_fields": {"送货单号": "SH-20260812-07", "货物名称": "螺纹钢 HRB400",
                        "数量": "12.5 吨"},
    },
    {
        "source": "sample_train_ticket.png",
        "doc_type": "ticket",
        "title": "铁路电子客票行程单-北京南至上海虹桥",
        "main_amount": 553.00,
        "main_date": "2026-08-05",
        "main_entity": "中国铁路",
        "type_fields": {"票号": "E123456789", "乘车人": "刘某某", "车次": "G1234次",
                        "区间": "北京南 → 上海虹桥", "发车时间": "2026-08-05 08:20"},
    },
    {
        "source": "sample_contract_6p.pdf",
        "doc_type": "contract",
        "title": "机械设备采购合同",
        "main_amount": 120000.00,
        "main_date": "2026-08-01",
        "main_entity": "某某机械制造有限公司",
        "type_fields": {"合同编号": "HT-2026-01", "甲方": "某某建设集团有限公司",
                        "乙方": "某某机械制造有限公司",
                        "合同标的": "甲方向乙方采购机械设备一批",
                        "条款范围": "交付、验收、质保与违约责任等通用条款"},
    },
]


def ensure_samples() -> None:
    missing = [d["source"] for d in DEMO_DOCS if not (SAMPLE_DIR / d["source"]).exists()]
    if missing:
        print(f"样例原件缺失 {len(missing)} 个，正在重新生成: {missing}")
        make_samples.make_all()


def seed(db: DatabaseManager, reset: bool = False, force: bool = False) -> int:
    existing = db.query_documents("SELECT COUNT(*) AS c FROM documents;")[0]["c"]
    if existing and reset:
        with db.get_connection() as conn:
            conn.execute("DELETE FROM documents;")
            conn.commit()
        print(f"已清空 documents 表 (原有 {existing} 条记录)")
        existing = 0
    elif existing and not force:
        print(f"库中已有 {existing} 条单据，为避免重复灌入已跳过。")
        print("如需重置演示数据请执行: python3 seed_demo_data.py --reset")
        return 0

    inserted = 0
    for doc in DEMO_DOCS:
        source = SAMPLE_DIR / doc["source"]
        rel_path, _ = MediaReader.save_and_archive(source)   # 按内容哈希归档到 data/attachments
        doc_id = db.insert_document(
            doc_type=doc["doc_type"],
            title=doc["title"],
            main_amount=doc["main_amount"],
            main_date=doc["main_date"],
            main_entity=doc["main_entity"],
            file_path=rel_path,
            extra_data={
                "doc_type": doc["doc_type"],
                "title": doc["title"],
                "main_amount": doc["main_amount"],
                "main_date": doc["main_date"],
                "main_entity": doc["main_entity"],
                "type_fields": doc.get("type_fields", {}),
                "other_type_name": "",
                "reply": doc.get("reply", ""),
            },
        )
        inserted += 1
        print(f"  #{doc_id}  [{doc['doc_type']:8s}] {doc['title']}  ¥{doc['main_amount']:.2f}  {doc['main_date']}")
    return inserted


def main() -> None:
    parser = argparse.ArgumentParser(description="灌入演示单据数据")
    parser.add_argument("--reset", action="store_true", help="先清空 documents 表再灌入")
    parser.add_argument("--force", action="store_true", help="库中已有数据时也继续追加")
    args = parser.parse_args()

    ensure_samples()
    db = DatabaseManager()
    count = seed(db, reset=args.reset, force=args.force)

    if count:
        print(f"\n已灌入 {count} 条演示单据。可以开始提问了，例如:")
        print("  - 我 8 月一共花了多少钱？")
        print("  - 把晨光文具那张发票发给我")


if __name__ == "__main__":
    main()
