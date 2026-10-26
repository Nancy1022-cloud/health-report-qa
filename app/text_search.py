"""文档和问题共用的中文分词；这里只做关键词匹配，不进行模型改写。"""

import json
import logging
import re
import unicodedata

import jieba

SEARCH_VERSION = "jieba_search_v1"
DOMAIN_WORDS = (
    "空腹血糖", "总胆固醇", "甘油三酯", "检查结论", "建议与备注",
    "复查建议", "肾功能", "尿酸", "用药方案", "糖化血红蛋白",
)
STOPWORDS = set("的 了 和 与 及 是 在 有 吗 呢 请 请问 多少 什么 是什么 哪些 "
                "爸爸 妈妈 父亲 母亲 报告 本次 这份 那份 这次 上次 具体 分别 列出 "
                "里面 里 中 检查 日期 年 月 日 一下".split())
SINGLE_TERMS = set("钾钠氯钙镁磷铁锌")
PARTS = re.compile(r"[\u4e00-\u9fff]+|[A-Za-z][A-Za-z0-9]*(?:[-_][A-Za-z0-9]+)*|\d+(?:\.\d+)?")
jieba.setLogLevel(logging.WARNING)
TOKENIZER = jieba.Tokenizer()
for word in DOMAIN_WORDS:
    TOKENIZER.add_word(word, freq=100000)


def tokenize(text):
    # 仅规范化检索副本；原文和引用位置不变。缩写保留为完整 token。
    text = unicodedata.normalize("NFKC", text).lower().replace("−", "-").replace("–", "-")
    result = []
    for match in PARTS.finditer(text):
        part = match.group()
        pieces = TOKENIZER.cut_for_search(part, HMM=False) if re.fullmatch(r"[\u4e00-\u9fff]+", part) else [part]
        for token in pieces:
            if token in STOPWORDS:
                continue
            if len(token) == 1 and "\u4e00" <= token <= "\u9fff" and token not in SINGLE_TERMS:
                continue
            result.append(token)
    return result


def query_terms(question):
    return list(dict.fromkeys(tokenize(question)))[:64]


def query_expression(terms):
    # token 来自白名单正则，不含引号、括号或用户输入的运算符。
    if any(not re.fullmatch(r"[\u4e00-\u9fffA-Za-z0-9_.-]+", term) for term in terms):
        raise ValueError("非法关键词。")
    return " | ".join("'" + term + "'" for term in terms)


if __name__ == "__main__":
    sample = "爸爸的空腹血糖、HbA1c 和 LDL-C 是多少？"
    terms = query_terms(sample)
    print(json.dumps({"question": sample, "tokens": terms, "tsquery": query_expression(terms)}, ensure_ascii=False, indent=2))