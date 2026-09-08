# Python port of Pi edit-diff.ts matching and unchanged-line preservation (MIT).
from __future__ import annotations

import re
import unicodedata
from bisect import bisect_right


# 按上游规则归一化尾空格、兼容字符、引号、横线和 Unicode 空格以匹配模型文本。
def normalize_for_match(text: str) -> str:
    text = "\n".join(line.rstrip() for line in unicodedata.normalize("NFKC", text).split("\n"))
    text = re.sub("[\u2018-\u201b]", "'", text)
    text = re.sub("[\u201c-\u201f]", '"', text)
    text = re.sub("[\u2010-\u2015\u2212]", "-", text)
    return re.sub("[\u00a0\u2002-\u200a\u202f\u205f\u3000]", " ", text)


# 基于同一原文匹配全部修改，模糊匹配只重写实际触及的行而保留其他行原貌。
def replace_regions(original: str, edits: list[tuple[str, str]]) -> str:
    fuzzy = any(old not in original for old, _ in edits)
    base = normalize_for_match(original) if fuzzy else original
    matches: list[tuple[int, int, str]] = []
    for old, new in edits:
        match = old if old in base else normalize_for_match(old)
        if not match or normalize_for_match(base).count(normalize_for_match(old)) != 1:
            raise ValueError("Each oldText must match exactly one region of the original file.")
        start = base.find(match)
        if start < 0:
            raise ValueError("oldText was not found in the original file.")
        matches.append((start, start + len(match), new))
    matches.sort()
    for previous, current in zip(matches, matches[1:]):
        if previous[1] > current[0]:
            raise ValueError("Edits overlap. Merge nearby changes into one replacement.")
    if not fuzzy:
        for start, end, new in reversed(matches):
            base = base[:start] + new + base[end:]
        return base
    original_lines = re.findall(r"[^\n]*\n|[^\n]+", original)
    base_lines = re.findall(r"[^\n]*\n|[^\n]+", base)
    if len(original_lines) != len(base_lines):
        raise ValueError("Normalized content has a different line count.")
    offsets = [0]
    for line in base_lines:
        offsets.append(offsets[-1] + len(line))
    groups: list[tuple[int, int, list[tuple[int, int, str]]]] = []
    for region in matches:
        first = bisect_right(offsets, region[0]) - 1
        last = bisect_right(offsets, region[1] - 1)
        if groups and first < groups[-1][1]:
            previous_first, previous_last, grouped = groups[-1]
            groups[-1] = (previous_first, max(previous_last, last), [*grouped, region])
        else:
            groups.append((first, last, [region]))
    output: list[str] = []
    cursor = 0
    for first, last, grouped in groups:
        output.extend(original_lines[cursor:first])
        fragment = base[offsets[first] : offsets[last]]
        for start, end, new in reversed(grouped):
            fragment = fragment[: start - offsets[first]] + new + fragment[end - offsets[first] :]
        output.append(fragment)
        cursor = last
    output.extend(original_lines[cursor:])
    return "".join(output)
