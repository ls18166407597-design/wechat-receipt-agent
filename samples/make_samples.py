"""
生成演示用样例单据 (真实可识别的图片与 PDF，用于演示与自动化测试)

用法: python3 samples/make_samples.py
产物:
  sample_invoice.png          发票        (invoice)
  sample_order_screenshot.png 购物订单    (order)
  sample_receipt.png          餐饮小票    (receipt)
  sample_contract_6p.pdf      6 页合同    (contract)
  sample_delivery_note.png    送货单      (delivery)
  sample_train_ticket.png     火车票行程  (ticket)
"""
from pathlib import Path

import fitz

OUT_DIR = Path(__file__).resolve().parent
FONT = "china-s"          # PyMuPDF 内置简体中文字体
RENDER_DPI = 150


def _write_png(name: str, title: str, lines: list[tuple[int, str]]) -> Path:
    """把文本渲染成一张图片 (先排成单页 PDF 再渲染为 PNG)"""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((50, 70), title, fontname=FONT, fontsize=17)
    for y, text in lines:
        page.insert_text((50, y), text, fontname=FONT, fontsize=12)
    tmp_pdf = OUT_DIR / f"_{name}.pdf"
    doc.save(str(tmp_pdf))
    doc.close()

    pix = fitz.open(str(tmp_pdf))[0].get_pixmap(dpi=RENDER_DPI)
    target = OUT_DIR / name
    pix.save(str(target))
    tmp_pdf.unlink()
    return target


def make_invoice() -> Path:
    return _write_png("sample_invoice.png", "增值税电子普通发票", [
        (110, "发票号码: 02514789"),
        (140, "销售方名称: 上海晨光办公用品有限公司"),
        (165, "货物或应税劳务名称: 办公用品"),
        (200, "金额(不含税): 1000.00"),
        (225, "税率: 13%    税额: 130.00"),
        (250, "价税合计(小写): 1130.00"),
        (285, "开票日期: 2026-08-18"),
    ])


def make_order() -> Path:
    return _write_png("sample_order_screenshot.png", "订单详情", [
        (110, "订单号: 2088123456789012"),
        (140, "店铺名称: 罗技旗舰店"),
        (165, "商品名称: 罗技 MX Keys 无线键盘"),
        (195, "实付款: 399.00 元"),
        (225, "下单时间: 2026-08-22 21:03"),
    ])


def make_receipt() -> Path:
    return _write_png("sample_receipt.png", "某某餐饮有限公司 消费小票", [
        (110, "单号: NO.20260815-0371"),
        (140, "宫保鸡丁          58.00"),
        (165, "米饭               6.00"),
        (190, "可乐              24.50"),
        (225, "合计: 88.50 元"),
        (255, "时间: 2026-08-15 12:41"),
    ])


def make_delivery_note() -> Path:
    return _write_png("sample_delivery_note.png", "材料送货单", [
        (110, "送货单号: SH-20260812-07"),
        (140, "供货单位: 某某建材有限公司"),
        (165, "货物名称: 螺纹钢 HRB400"),
        (195, "数量: 12.5 吨"),
        (225, "金额: 51250.00 元"),
        (255, "收货日期: 2026-08-12"),
    ])


def make_train_ticket() -> Path:
    return _write_png("sample_train_ticket.png", "铁路电子客票 行程单", [
        (110, "票号: E123456789"),
        (140, "乘车人: 刘某某"),
        (165, "车次: G1234 次"),
        (190, "区间: 北京南 -> 上海虹桥"),
        (220, "发车时间: 2026-08-05 08:20"),
        (250, "票价: 553.00 元"),
    ])


def make_contract(pages: int = 6) -> Path:
    doc = fitz.open()
    cover = doc.new_page()
    for y, text in [
        (100, "机械设备采购合同"),
        (140, "合同编号: HT-2026-01"),
        (170, "甲方: 某某建设集团有限公司"),
        (200, "乙方: 某某机械制造有限公司"),
        (240, "合同总金额: 120000.00 元"),
        (270, "签订日期: 2026-08-01"),
        (310, "第一条 合同标的：甲方向乙方采购机械设备一批。"),
    ]:
        cover.insert_text((50, y), text, fontname=FONT, fontsize=12)

    for i in range(2, pages):
        page = doc.new_page()
        page.insert_text((50, 100), f"第 {i} 条 通用条款", fontname=FONT, fontsize=12)
        page.insert_text((50, 130), "双方就交付、验收、质保与违约责任作出如下约定……",
                         fontname=FONT, fontsize=11)

    sign = doc.new_page()
    sign.insert_text((50, 100), "签署页", fontname=FONT, fontsize=12)
    sign.insert_text((50, 140), "甲方(盖章): 某某建设集团有限公司", fontname=FONT, fontsize=11)
    sign.insert_text((50, 170), "乙方(盖章): 某某机械制造有限公司", fontname=FONT, fontsize=11)
    sign.insert_text((50, 200), "签署日期: 2026-08-01", fontname=FONT, fontsize=11)

    target = OUT_DIR / "sample_contract_6p.pdf"
    doc.save(str(target))
    doc.close()
    return target


def make_all() -> list[Path]:
    return [
        make_invoice(),
        make_order(),
        make_receipt(),
        make_contract(),
        make_delivery_note(),
        make_train_ticket(),
    ]


if __name__ == "__main__":
    for path in make_all():
        print(f"已生成 {path.name} ({path.stat().st_size} 字节)")
