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
        # unnumbered pages (unreadable 'Strana x z y') take the free page slots in input order
        taken = {p.get("page_number") for p in g["pages"] if p.get("page_number")}
        free = iter(n for n in range(1, len(g["pages"]) + 1) if n not in taken)
        slot = {id(p): (p.get("page_number") or next(free, 10_000)) for p in sorted(g["pages"], key=lambda p: p["_order"])}
        g["pages"].sort(key=lambda p: (slot[id(p)], p["_order"]))
        for p in g["pages"]:
            p.pop("_order", None)
    return groups


def group_v2_pages(pages: list[dict]) -> list[dict]:
    """Group /classify_json V2 page observations (printed page numbers, group_hint) into documents."""
    groups = group_documents([{"source_file": p["source_file"], "page": p["page"], "doc_type": p["document_type"],
                               "doc_key": p.get("group_hint"), "page_number": p.get("printed_page_number"),
                               "page_count": p.get("printed_page_count"), "hint": p.get("group_hint"),
                               "confidence": p["confidence"], "reason": p.get("reason", "")} for p in pages])
    return _merge_complementary(groups)


def _ids(group) -> set:
    """Identifier-like digit runs (account, IČO, card or reference numbers) seen on a group's pages."""
    out = set()
    for p in group["pages"]:
        for run in re.findall(r"\d[\d \-/]{4,}\d", p.get("hint") or ""):
            digits = re.sub(r"\D", "", run)
            if len(digits) >= 6:
                out.add(digits)
    return out


def _numbers(group):
    nums = [p.get("page_number") for p in group["pages"]]
    counts = {p.get("page_count") for p in group["pages"]}
    return (set(nums), counts.pop()) if None not in nums and len(counts) == 1 and None not in counts else (None, None)


def _merge_complementary(groups: list[dict]) -> list[dict]:
    """Free-text group hints differ page to page ('VÝPIS Z ÚČTU' vs 'Account 2329-…'), so shuffled
    pages of one document land in several groups. Merge same-type groups of one file whose printed
    page numbers complete each other within the same 'of N', unless both carry identifiers and
    share none (two different documents). Complete or overlapping groups are never merged."""
    out: list[dict] = []
    for g in groups:
        nums, count = _numbers(g)
        target = None
        if nums is not None:
            for t in out:
                tn, tc = _numbers(t)
                if (t["doc_type"] == g["doc_type"] and t["source_file"] == g["source_file"] and tc == count
                        and tn is not None and not tn & nums and len(tn | nums) <= count
                        and not (_ids(t) and _ids(g) and not _ids(t) & _ids(g))):
                    target = t
                    break
        if target is None:
            out.append(g)
            continue
        target["pages"] = sorted(target["pages"] + g["pages"], key=lambda p: p["page_number"])
        target["confidence"] = min(target["confidence"], g["confidence"])
    for i, g in enumerate(out):
        g["doc_id"] = f"d{i + 1}"
    return out
