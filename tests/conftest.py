"""确保测试可以直接 import 项目模块与 samples（无论从哪个目录调用 pytest）"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for path in (ROOT, ROOT / "samples"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


@pytest.fixture
def archive_dir(tmp_path, monkeypatch):
    """把归档目录指到 tmp 下，让上传类测试走真实路径。

    handle_document_upload 只接受附件目录内的文件（/api/message 无鉴权，
    filePath 由客户端传入，不设这道校验就是任意文件读取口）。
    上传测试必须把文件放进真正的附件目录，否则测的是被拒绝的分支。
    """
    from core.config import Config
    d = tmp_path / "attachments"
    d.mkdir()
    monkeypatch.setattr(Config, "ATTACHMENTS_DIR", d)
    return d
