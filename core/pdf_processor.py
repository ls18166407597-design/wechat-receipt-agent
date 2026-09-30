"""
多页 PDF 取页与多模态素材准备

取页策略: **只读开头若干页，后面的不读**。
- 短文档 (页数不超过上限): 全读
- 长文档: 只读第 1 ~ N 页

为什么不做"该读哪几页"的启发式(比如首尾页、目录页、签章页): 本项目记的是单据，
不是合同台账。一份 20 页的合同，标的、金额、日期、双方这些要归档的信息在首页就齐了，
后面几十页通用条款对归档没有增量；而长对账单这种"关键行在中间页"的文档，
任何固定采样都救不了——不如把边界划死，再在回复里如实说明只读了前几页。

多页 PDF 用 PyMuPDF 把要读的页按指定 DPI 渲染为 PNG 后送入视觉模型
(多数 OpenAI 兼容接口不直接接受 PDF 原始字节)。
"""
from pathlib import Path
from typing import List, Tuple

from .config import Config


class PDFProcessor:
    @staticmethod
    def sample_pdf_pages(total_pages: int, max_sample_pages: int = Config.PDF_MAX_SAMPLE_PAGES) -> List[int]:
        """返回要读的页码列表 (1-based): 从第 1 页起，最多 max_sample_pages 页"""
        if total_pages <= 0:
            return []
        return list(range(1, min(total_pages, max_sample_pages) + 1))

    @staticmethod
    def get_page_count(file_path: str | Path) -> int:
        fitz = PDFProcessor._fitz()
        try:
            with fitz.open(str(file_path)) as doc:
                return doc.page_count
        except Exception as e:
            raise ValueError(f"无法解析 PDF 文件: {e}") from e

    @classmethod
    def render_sampled_pages(cls, file_path: str | Path,
                             dpi: int = Config.PDF_RENDER_DPI) -> Tuple[int, List[int], List[bytes]]:
        """
        读取 PDF 页数 -> 采样关键页 -> 逐页渲染为 PNG
        :return: (总页数, 采样页码列表, 每页 PNG 字节列表) —— 后两者一一对应
        """
        fitz = cls._fitz()
        total = cls.get_page_count(file_path)
        pages = cls.sample_pdf_pages(total)

        images: List[bytes] = []
        try:
            with fitz.open(str(file_path)) as doc:
                for page_no in pages:
                    pix = doc.load_page(page_no - 1).get_pixmap(dpi=dpi)
                    images.append(pix.tobytes("png"))
        except Exception as e:
            raise ValueError(f"PDF 页面渲染失败: {e}") from e
        return total, pages, images

    @staticmethod
    def _fitz():
        try:
            import fitz
        except ImportError as e:
            raise RuntimeError("解析 PDF 需要 PyMuPDF，请先执行: pip install -r requirements.txt") from e
        return fitz
