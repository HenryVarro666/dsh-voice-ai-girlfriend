"""Explicit experimental number fields; never rewrite arbitrary user text."""

import re

DIGITS = "零一二三四五六七八九"
VALUES = {c: i for i, c in enumerate(DIGITS)} | {"〇": 0, "幺": 1, "两": 2}
ALPHABET = "0123456789零〇幺一二两三四五六七八九十百千万"
TOKEN = f"[{ALPHABET}]+"


def spoken_integer(value):
    if not 0 <= value < 100:
        raise ValueError("Only month/day/hour/minute values supported")
    if value < 10:
        return DIGITS[value]
    tens, ones = divmod(value, 10)
    return ("" if tens == 1 else DIGITS[tens]) + "十" + (DIGITS[ones] if ones else "")


def render(case, normalized):
    code = case["code"]
    if not re.fullmatch(r"[0-9]+", code):
        raise ValueError("Code must be an ASCII digit string")
    read = "".join(DIGITS[int(d)] for d in code) if normalized else code
    text = f"编号是{read}。"
    if "date" in case:
        year, month, day = case["date"]
        hour, minute = case["time"]
        y = "".join(DIGITS[int(d)] for d in str(year)) if normalized else str(year)
        nums = [
            spoken_integer(n) if normalized else str(n)
            for n in (month, day, hour, minute)
        ]
        m, d, h, minute_text = nums
        text += f"日期是{y}年{m}月{d}日，时间是{h}点{minute_text}分。"
    return text


def integer(text):
    if text.isascii() and text.isdigit():
        return int(text)
    if all(c in VALUES for c in text):
        return int("".join(str(VALUES[c]) for c in text))
    # Require a conventional cardinal grammar; reject e.g. 一百零二二.
    pattern = r"(?:[一二两三四五六七八九]千)?(?:零?[一二两三四五六七八九]百)?(?:零?(?:[一二两三四五六七八九]?十))?(?:零?[一二两三四五六七八九])?"
    if not text or not re.fullmatch(pattern, text):
        return None
    total, pending = 0, None
    for c in text:
        if c in VALUES:
            pending = VALUES[c]
        else:
            total += (pending if pending is not None else 1) * {
                "十": 10,
                "百": 100,
                "千": 1000,
            }[c]
            pending = None
    return total + (pending or 0)


def check(case, transcript):
    matches = list(
        re.finditer(
            r"编号(?:是|为)?(.*?)(?=日期|时间|[。！？.!?，,；;\n]|$)", transcript
        )
    )
    result = {"expected_code": case["code"], "code": None, "fields": {}}
    if len(matches) == 1:
        token = re.sub(r"\s+", "", matches[0][1])
        if token and all(c in VALUES or c.isascii() and c.isdigit() for c in token):
            result["code"] = "".join(
                str(VALUES[c]) if c in VALUES else c for c in token
            )
        result["code_token"] = token
    result["fields"]["code"] = (
        "unparsed"
        if result["code"] is None
        else "match"
        if result["code"] == case["code"]
        else "mismatch"
    )
    if "date" in case:
        patterns = {
            "date": rf"({TOKEN})年({TOKEN})月({TOKEN})[日号]",
            "time": rf"({TOKEN})[点时]({TOKEN})分",
        }
        for field, pattern in patterns.items():
            found = list(re.finditer(pattern, transcript))
            values = [integer(t) for t in found[0].groups()] if len(found) == 1 else []
            result[field] = values
            result["fields"][field] = (
                "unparsed"
                if not values or None in values
                else "match"
                if values == case[field]
                else "mismatch"
            )
    result["status"] = (
        "unparsed"
        if "unparsed" in result["fields"].values()
        else "mismatch"
        if "mismatch" in result["fields"].values()
        else "match"
    )
    return result
