#!/usr/bin/env bash
# Deploy KB extractor to Lambda + Function URL in eu-central-1.
# Pay-per-call only. No VPC/NAT/API Gateway.
set -euo pipefail
REGION=eu-central-1
ACCOUNT=$(aws sts get-caller-identity --query Account --output text --region "$REGION")
NAME=fintechers-kb-extractor
ROLE_NAME=${NAME}-role
TAGS="Key=project,Value=fintechers-kb Key=owner,Value=carlos"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD="$ROOT/deploy/build"
ZIP="$ROOT/deploy/package.zip"
TOKEN_FILE="$ROOT/.deploy_token"

export AWS_DEFAULT_REGION="$REGION"
export AWS_REGION="$REGION"

echo "==> Account $ACCOUNT region $REGION"

# Fail fast on expired token
aws sts get-caller-identity --region "$REGION" >/dev/null

# API key
if [[ -f "$TOKEN_FILE" ]]; then
  API_KEY=$(cat "$TOKEN_FILE")
else
  API_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
  umask 077
  printf '%s' "$API_KEY" > "$TOKEN_FILE"
  echo "Wrote API token to $TOKEN_FILE (gitignored)"
fi

echo "==> Building Lambda zip (manylinux py3.12 x86_64)"
rm -rf "$BUILD" "$ZIP"
mkdir -p "$BUILD"
python3 -m pip install -q --target "$BUILD" \
  --platform manylinux2014_x86_64 --python-version 3.12 --only-binary=:all: \
  fastapi 'mangum>=0.17' python-multipart pymupdf Pillow PyYAML \
  pydantic pydantic-core starlette anyio sniffio typing-extensions annotated-types
# app code (boto3 comes from Lambda runtime)
cp -a "$ROOT/extractor" "$BUILD/extractor"
# drop caches / tests / samples from package
find "$BUILD" -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true
find "$BUILD" -name '*.pyc' -delete
rm -rf "$BUILD/extractor/samples" 2>/dev/null || true
# zip
( cd "$BUILD" && python3 - <<'PY'
import zipfile, os
from pathlib import Path
out = Path("../package.zip")
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for p in Path(".").rglob("*"):
        if p.is_file():
            z.write(p, p.as_posix())
print("zip_mb", round(out.stat().st_size/1e6, 2))
PY
)

ZIP_MB=$(python3 -c "import os; print(round(os.path.getsize('$ZIP')/1e6,2))")
echo "Package $ZIP_MB MB"

# IAM role
TRUST='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
POLICY=$(cat <<POL
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "Logs",
      "Effect": "Allow",
      "Action": ["logs:CreateLogGroup","logs:CreateLogStream","logs:PutLogEvents"],
      "Resource": "arn:aws:logs:${REGION}:${ACCOUNT}:*"
    },
    {
      "Sid": "Textract",
      "Effect": "Allow",
      "Action": ["textract:AnalyzeDocument","textract:DetectDocumentText"],
      "Resource": "*"
    },
    {
      "Sid": "Bedrock",
      "Effect": "Allow",
      "Action": ["bedrock:InvokeModel","bedrock:InvokeModelWithResponseStream","bedrock:Converse","bedrock:ConverseStream"],
      "Resource": [
        "arn:aws:bedrock:${REGION}:${ACCOUNT}:inference-profile/*",
        "arn:aws:bedrock:*::foundation-model/anthropic.*",
        "arn:aws:bedrock:*::foundation-model/amazon.nova-*"
      ]
    }
  ]
}
POL
)

if ! aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
  echo "==> Creating role $ROLE_NAME"
  aws iam create-role --role-name "$ROLE_NAME" --assume-role-policy-document "$TRUST" \
    --tags Key=project,Value=fintechers-kb Key=owner,Value=carlos >/dev/null
  aws iam put-role-policy --role-name "$ROLE_NAME" --policy-name ${NAME}-inline --policy-document "$POLICY"
  echo "Waiting for IAM propagation…"
  sleep 12
else
  echo "==> Updating role policy"
  aws iam put-role-policy --role-name "$ROLE_NAME" --policy-name ${NAME}-inline --policy-document "$POLICY"
  aws iam tag-role --role-name "$ROLE_NAME" --tags Key=project,Value=fintechers-kb Key=owner,Value=carlos 2>/dev/null || true
fi
ROLE_ARN=$(aws iam get-role --role-name "$ROLE_NAME" --query Role.Arn --output text)

ENV_JSON=$(python3 - <<PY
import json
print(json.dumps({"Variables":{
  "EXTRACTOR_API_KEY": """$API_KEY""",
  "AWS_REGION_EXTRACTOR": "eu-central-1",
  "BEDROCK_MODEL": "eu.anthropic.claude-sonnet-4-5-20250929-v1:0",
  "BEDROCK_CLASSIFY_MODEL": "eu.anthropic.claude-haiku-4-5-20251001-v1:0",
  "EXTRACTOR_BUDGET_USD": "5.0",
}}))
PY
)

if aws lambda get-function --function-name "$NAME" >/dev/null 2>&1; then
  echo "==> Updating function code"
  if python3 -c "import os; raise SystemExit(0 if os.path.getsize('$ZIP') < 50_000_000 else 1)"; then
    aws lambda update-function-code --function-name "$NAME" --zip-file "fileb://$ZIP" >/dev/null
  else
    # direct upload limit 50MB — use temporary S3 object then delete
    BUCKET="${NAME}-pkg-${ACCOUNT}"
    if ! aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null; then
      aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" \
        --create-bucket-configuration LocationConstraint="$REGION" \
        --object-ownership BucketOwnerEnforced >/dev/null
      aws s3api put-bucket-tagging --bucket "$BUCKET" --tagging 'TagSet=[{Key=project,Value=fintechers-kb},{Key=owner,Value=carlos}]'
    fi
    KEY="package-$(date +%s).zip"
    aws s3 cp "$ZIP" "s3://$BUCKET/$KEY" --region "$REGION"
    aws lambda update-function-code --function-name "$NAME" --s3-bucket "$BUCKET" --s3-key "$KEY" >/dev/null
    aws s3 rm "s3://$BUCKET/$KEY"
    # empty + delete bucket
    aws s3 rb "s3://$BUCKET" --force 2>/dev/null || true
    echo "Temporary S3 packaging bucket deleted"
  fi
  aws lambda wait function-updated --function-name "$NAME"
  aws lambda update-function-configuration --function-name "$NAME" \
    --timeout 180 --memory-size 1024 --runtime python3.12 \
    --handler extractor.lambda_handler.handler \
    --role "$ROLE_ARN" \
    --environment "$ENV_JSON" \
    --tags project=fintechers-kb,owner=carlos >/dev/null || \
  aws lambda update-function-configuration --function-name "$NAME" \
    --timeout 180 --memory-size 1024 --runtime python3.12 \
    --handler extractor.lambda_handler.handler \
    --role "$ROLE_ARN" \
    --environment "$ENV_JSON" >/dev/null
  aws lambda wait function-updated --function-name "$NAME"
else
  echo "==> Creating function"
  CREATE_ARGS=(--function-name "$NAME" --runtime python3.12
    --role "$ROLE_ARN" --handler extractor.lambda_handler.handler
    --timeout 180 --memory-size 1024
    --environment "$ENV_JSON"
    --tags project=fintechers-kb,owner=carlos)
  if python3 -c "import os; raise SystemExit(0 if os.path.getsize('$ZIP') < 50_000_000 else 1)"; then
    aws lambda create-function "${CREATE_ARGS[@]}" --zip-file "fileb://$ZIP" >/dev/null
  else
    BUCKET="${NAME}-pkg-${ACCOUNT}"
    if ! aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null; then
      aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" \
        --create-bucket-configuration LocationConstraint="$REGION" \
        --object-ownership BucketOwnerEnforced >/dev/null
      aws s3api put-bucket-tagging --bucket "$BUCKET" --tagging 'TagSet=[{Key=project,Value=fintechers-kb},{Key=owner,Value=carlos}]'
    fi
    KEY="package.zip"
    aws s3 cp "$ZIP" "s3://$BUCKET/$KEY" --region "$REGION"
    aws lambda create-function "${CREATE_ARGS[@]}" --code S3Bucket="$BUCKET",S3Key="$KEY" >/dev/null
    aws s3 rm "s3://$BUCKET/$KEY"
    aws s3 rb "s3://$BUCKET" --force 2>/dev/null || true
    echo "Temporary S3 packaging bucket deleted"
  fi
  aws lambda wait function-active --function-name "$NAME"
fi

# tags on function (update-function-configuration --tags not always valid)
aws lambda tag-resource --resource "arn:aws:lambda:${REGION}:${ACCOUNT}:function:${NAME}" \
  --tags project=fintechers-kb,owner=carlos 2>/dev/null || true

echo "==> Reserved concurrency = 2"
aws lambda put-function-concurrency --function-name "$NAME" --reserved-concurrent-executions 2 >/dev/null

echo "==> Log group retention 7 days"
aws logs create-log-group --log-group-name "/aws/lambda/$NAME" --region "$REGION" 2>/dev/null || true
aws logs put-retention-policy --log-group-name "/aws/lambda/$NAME" --retention-in-days 7 --region "$REGION"
aws logs tag-log-group --log-group-name "/aws/lambda/$NAME" --tags project=fintechers-kb,owner=carlos 2>/dev/null || true

echo "==> Function URL (AuthType NONE; app checks x-api-key)"
if ! aws lambda get-function-url-config --function-name "$NAME" >/dev/null 2>&1; then
  aws lambda create-function-url-config --function-name "$NAME" \
    --auth-type NONE \
    --cors 'AllowOrigins=*,AllowMethods=GET,POST,AllowHeaders=content-type,x-api-key,AllowCredentials=false' >/dev/null
  aws lambda add-permission --function-name "$NAME" \
    --statement-id FunctionURLAllowPublicAccess \
    --action lambda:InvokeFunctionUrl \
    --principal "*" \
    --function-url-auth-type NONE >/dev/null 2>&1 || true
fi
URL=$(aws lambda get-function-url-config --function-name "$NAME" --query FunctionUrl --output text)
printf '%s\n' "$URL" > "$ROOT/deploy/function_url.txt"

echo ""
echo "Deployed."
echo "Function: $NAME"
echo "URL: $URL"
echo "Token file: $TOKEN_FILE"
echo "Test:"
echo "  curl -s ${URL}health"
echo "  curl -s -H \"x-api-key: \$(cat $TOKEN_FILE)\" -F files=@samples/02_missing_payslip/id_card.pdf ${URL}extract"
