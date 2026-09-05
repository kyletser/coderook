import re


# 将由 h、m、s 组成的时长字符串解析为总秒数
def parse_duration(value: str) -> int:
    match = re.fullmatch(r"(\d+)([hms])", value.strip())
    if match is None:
        raise ValueError("invalid duration")
    return int(match.group(1))
