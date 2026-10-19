import argparse
import json
from pathlib import Path

from .pipeline import run_path


def main(argv=None):
    p = argparse.ArgumentParser(description="KB back-office document extractor")
    p.add_argument("path", help="PDF/image file or folder of files")
    p.add_argument("-o", "--out", help="Write JSON to this file (default: stdout)")
    p.add_argument("--explain", action="store_true",
                   help="Ask Bedrock for a one-sentence explanation of disagreements")
    args = p.parse_args(argv)
    result = run_path(args.path, explain=args.explain)
    text = json.dumps(result, indent=2, ensure_ascii=False)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {args.out}  next={result['recommended_next_step']}  "
              f"auto={result['fields_auto_validated_pct']}%  "
              f"missing={result['missing_documents']}")
    else:
        print(text)
    return 0
