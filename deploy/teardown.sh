#!/usr/bin/env bash
# Tear down Lambda + role + log group. Leaves nothing billable.
set -euo pipefail
REGION=eu-central-1
NAME=fintechers-kb-extractor
ROLE_NAME=${NAME}-role
export AWS_DEFAULT_REGION="$REGION"
export AWS_REGION="$REGION"

ACCOUNT=$(aws sts get-caller-identity --query Account --output text --region "$REGION")

echo "==> Delete Function URL + function"
aws lambda delete-function-url-config --function-name "$NAME" 2>/dev/null || true
aws lambda delete-function --function-name "$NAME" 2>/dev/null || true

echo "==> Delete log group"
aws logs delete-log-group --log-group-name "/aws/lambda/$NAME" 2>/dev/null || true

echo "==> Delete IAM role policy + role"
aws iam delete-role-policy --role-name "$ROLE_NAME" --policy-name ${NAME}-inline 2>/dev/null || true
aws iam delete-role --role-name "$ROLE_NAME" 2>/dev/null || true

# stray packaging bucket if any
BUCKET="${NAME}-pkg-${ACCOUNT}"
aws s3 rb "s3://$BUCKET" --force 2>/dev/null || true

rm -f "$(cd "$(dirname "$0")" && pwd)/function_url.txt"
echo "Teardown complete. Token file .deploy_token left for you to delete manually if desired."
