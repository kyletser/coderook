import os
import tempfile
from pathlib import Path


# 在目标目录创建临时文件并用原子替换提交完整内容
def write_atomic(path: Path, content: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
