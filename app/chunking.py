"""两种可切换切分；所有位置始终对应未修改的 raw_text。"""

import re

CHUNK_SIZE = 200
CHUNK_OVERLAP = 40
VERSIONS = {"fixed": "fixed200o40", "section": "section200o40v1"}
HEADING = re.compile(r"(?m)^[ \t]*【([^】\r\n]{1,40})】[ \t]*\r?$")


def _windows(text, start, end, section=None):
    result = []
    while start < end:
        stop = min(start + CHUNK_SIZE, end)
        if text[start:stop].strip():
            result.append({"start_char": start, "end_char": stop,
                           "content": text[start:stop], "section": section})
        if stop == end:
            break
        start = stop - CHUNK_OVERLAP
    return result


def chunk_text(text, strategy="fixed"):
    if strategy not in VERSIONS:
        raise ValueError("切分策略必须为 fixed 或 section。")
    if strategy == "fixed":
        return _windows(text, 0, len(text))
    headings = list(HEADING.finditer(text))
    if not headings:
        return _windows(text, 0, len(text))
    # 标题属于其后的章节；不拼接文本，不改变换行，也不跨章节重叠。
    result = _windows(text, 0, headings[0].start())
    for i, heading in enumerate(headings):
        end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
        result.extend(_windows(text, heading.start(), end, heading.group(1).strip()))
    return result