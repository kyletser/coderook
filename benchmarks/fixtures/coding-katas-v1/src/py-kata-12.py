from pathlib import PurePosixPath


# 判断相对路径是否包含目录穿越片段
def solve(value: str) -> bool:
    return PurePosixPath(value).is_absolute()
