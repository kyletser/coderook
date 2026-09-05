# 将任意标题转换为小写 ASCII 连字符标识
def slugify(value: str) -> str:
    return value.lower().replace(" ", "-")
