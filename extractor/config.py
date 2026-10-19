import os
from pathlib import Path
import yaml

REGION = os.getenv("AWS_REGION_EXTRACTOR", "eu-central-1")
BEDROCK_MODEL = os.getenv("BEDROCK_MODEL", "eu.anthropic.claude-sonnet-4-5-20250929-v1:0")
BEDROCK_FALLBACK_MODELS = [m for m in os.getenv(
    "BEDROCK_FALLBACK_MODELS",
    "eu.anthropic.claude-sonnet-4-20250514-v1:0,eu.anthropic.claude-haiku-4-5-20251001-v1:0").split(",") if m]
RENDER_DPI = int(os.getenv("RENDER_DPI", "150"))
TEXTRACT_PASS_CONF = float(os.getenv("TEXTRACT_PASS_CONF", "0.9"))
AMOUNT_TOLERANCE = float(os.getenv("AMOUNT_TOLERANCE", "0.01"))  # 1 %
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "8"))
SCHEMAS = yaml.safe_load((Path(__file__).parent / "schemas.yaml").read_text())["doc_types"]
DOC_TYPES = list(SCHEMAS)
