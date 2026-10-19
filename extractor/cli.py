"""Run the same classification or typed extraction operation as the HTTP API."""

import argparse
import json
from pathlib import Path

from . import document_models as D
from . import document_service


def parse_request(operation, payload):
    model = {"classify": D.ClassifyJsonRequest, "extract-typed": D.ExtractTypedJsonRequest}[operation]
    return model.model_validate(payload)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["classify", "extract-typed"])
    parser.add_argument("request", type=Path, help="JSON request using the corresponding HTTP contract")
    parser.add_argument("-o", "--out", type=Path)
    args = parser.parse_args(argv)
    try:
        request = parse_request(args.operation, json.loads(args.request.read_text()))
    except (ValueError, OSError, TypeError):
        parser.error("invalid parser request; provide the full classification or typed extraction contract")
    operation = document_service.discover if args.operation == "classify" else document_service.extract_typed
    result = operation(request)
    text = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0
