"""只融合名次，不直接相加向量距离和全文检索分数。"""

RRF_K = 60
CANDIDATE_K = 10


def fuse(vector_rows, keyword_rows, top_k):
    merged = {}
    for channel, rows in (("vector", vector_rows), ("keyword", keyword_rows)):
        seen = set()
        for rank, row in enumerate(rows, 1):
            source_id = row["source_id"]
            if source_id in seen:
                continue
            seen.add(source_id)
            item = merged.setdefault(source_id, {**row, "rrf_score": 0.0,
                                                "vector_rank": None, "keyword_rank": None})
            if "distance" in row:
                item["distance"] = row["distance"]
            if "keyword_score" in row:
                item["keyword_score"] = row["keyword_score"]
            item[channel + "_rank"] = rank
            item["rrf_score"] += 1.0 / (RRF_K + rank)
    return sorted(merged.values(), key=lambda row: (-row["rrf_score"], row["source_id"]))[:top_k]