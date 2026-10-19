"""Split a package of classified pages into logical documents (deterministic).

1. Group by (source_file, doc_type, doc_key). Pages without a doc_key join the most recent
   group of the same type in the same file (or start one).
2. A repeated page_number inside a group means a second document of the same type.
3. Inside a group, pages are ordered by printed page_number (unknown numbers keep input order).
Works for shuffled multi-doc PDFs; contiguity is NOT required.
"""
from __future__ import annotations

import re


def norm_key(k):
    if not k:
        return None
    k = re.sub(r"[^0-9A-Za-z]", "", str(k)).upper()
    return k or None


def group_documents(classified: list[dict]) -> list[dict]:
    groups: list[dict] = []
    for idx, c in enumerate(classified):
        key = norm_key(c.get("doc_key"))
        same = [g for g in groups if g["source_file"] == c["source_file"] and g["doc_type"] == c["doc_type"]]
        target = None
        if key:
            keyed = [g for g in same if g["doc_key"] == key]
            unkeyed = [g for g in same if g["doc_key"] is None]
            target = keyed[-1] if keyed else (unkeyed[-1] if unkeyed else None)
            if target is not None and target["doc_key"] is None:
                target["doc_key"] = key
        elif same:
            target = same[-1]
        pn = c.get("page_number")
        if target is not None and pn and any(p.get("page_number") == pn for p in target["pages"]):
            target = None  # same page number twice => another document of this type
        if target is None and pn and pn > 1 and groups:
            # 'page 2 of 2' never starts a document: if the page just before is the same type and
            # its document lacks this page number, continue it even if the keys were read
            # differently (noisy scan: the IBAN on page 1 misread by the classifier).
            prev = groups[-1] if groups[-1]["pages"] and groups[-1]["pages"][-1]["_order"] == idx - 1 else None
            if (prev is not None and prev["doc_type"] == c["doc_type"] and prev["source_file"] == c["source_file"]
                    and not any(p.get("page_number") == pn for p in prev["pages"])
                    and len(prev["pages"]) < pn):
                target = prev
        if target is None:
            target = {"doc_type": c["doc_type"], "source_file": c["source_file"], "doc_key": key,
                      "pages": [], "confidence": c["confidence"], "reason": c["reason"]}
            groups.append(target)
        target["pages"].append({**c, "_order": idx})
        target["confidence"] = min(target["confidence"], c["confidence"])

    for i, g in enumerate(groups):
        g["doc_id"] = f"d{i + 1}"
        g["pages"].sort(key=lambda p: (p.get("page_number") is None, p.get("page_number") or 0, p["_order"]))
        for p in g["pages"]:
            p.pop("_order", None)
    return groups
