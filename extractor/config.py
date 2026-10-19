"""Runtime config. Region is locked to Frankfurt unless AWS_REGION overrides it."""
import os
import sys
from pathlib import Path
import yaml

# Carlos requirement: default and only supported region is Frankfurt.
_DEFAULT_REGION = "eu-central-1"
_override = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")
if _override and _override != _DEFAULT_REGION:
    print(
        f"ERROR: extractor refuses to run outside Frankfurt. "
        f"Got AWS_REGION={_override!r}. Unset it or set AWS_REGION=eu-central-1.",
        file=sys.stderr,
    )
    raise SystemExit(2)
REGION = _DEFAULT_REGION

# On-demand foundation model IDs (anthropic.claude-...) are NOT invokable in
# eu-central-1 — Bedrock requires an inference profile. The eu.* profile is
# invoked FROM Frankfurt but Cross-Region Inference may process in other EU
# regions. Documented in NOTAS_CARLOS.md.
BEDROCK_MODEL = os.getenv("BEDROCK_MODEL", "eu.anthropic.claude-sonnet-4-5-20250929-v1:0")
BEDROCK_FALLBACK_MODELS = [m for m in os.getenv(
    "BEDROCK_FALLBACK_MODELS",
    "eu.anthropic.claude-sonnet-4-20250514-v1:0,eu.amazon.nova-lite-v1:0").split(",") if m]

RENDER_DPI = int(os.getenv("RENDER_DPI", "150"))
TEXTRACT_PASS_CONF = float(os.getenv("TEXTRACT_PASS_CONF", "0.9"))
AMOUNT_TOLERANCE = float(os.getenv("AMOUNT_TOLERANCE", "0.01"))  # 1 %
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "8"))
SCHEMAS = yaml.safe_load((Path(__file__).parent / "schemas.yaml").read_text())["doc_types"]
DOC_TYPES = list(SCHEMAS)
