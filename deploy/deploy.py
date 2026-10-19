#!/usr/bin/env python3
"""Deploy extractor to Lambda + Function URL in eu-central-1 (boto3)."""
from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

REGION = "eu-central-1"
NAME = "fintechers-kb-extractor"
ROLE_NAME = f"{NAME}-role"
TAGS = [{"Key": "project", "Value": "fintechers-kb"}, {"Key": "owner", "Value": "carlos"}]
TAG_MAP = {"project": "fintechers-kb", "owner": "carlos"}
ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "deploy" / "build"
ZIP = ROOT / "deploy" / "package.zip"
TOKEN_FILE = ROOT / ".deploy_token"
URL_FILE = ROOT / "deploy" / "function_url.txt"


def die(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def clients():
    os.environ["AWS_DEFAULT_REGION"] = REGION
    os.environ["AWS_REGION"] = REGION
    session = boto3.Session(region_name=REGION)
    try:
        ident = session.client("sts").get_caller_identity()
    except ClientError as e:
        if e.response["Error"]["Code"] in ("ExpiredToken", "ExpiredTokenException", "RequestExpired"):
            die("AWS credentials expired (ExpiredToken). Refresh SSO and retry.")
        raise
    print(f"Account {ident['Account']}  identity {ident['Arn']}")
    return session, ident["Account"]


# Set out of band by deploy_cloudfront.py / the demo-invoke role setup. update_function_configuration
# REPLACES the whole Environment, so these must be carried over or /session breaks.
PRESERVE_ENV = ("DEMO_INVOKE_ROLE_ARN", "FUNCTION_URL", "EXTRACTOR_API_KEY")


def existing_env(lam) -> dict:
    try:
        return lam.get_function_configuration(FunctionName=NAME).get("Environment", {}).get("Variables", {}) or {}
    except ClientError:
        return {}


def resolve_token(token_file: Path, current_env: dict) -> tuple[str, str]:
    """Never rotate the token Jose / the judges already have. Returns (token, source)."""
    if token_file.exists() and token_file.read_text().strip():
        return token_file.read_text().strip(), "file"
    if current_env.get("EXTRACTOR_API_KEY"):
        token, src = current_env["EXTRACTOR_API_KEY"], "lambda"
    else:
        token, src = secrets.token_urlsafe(32), "new"
    token_file.write_text(token)
    os.chmod(token_file, 0o600)
    return token, src


def merge_env(current: dict, ours: dict) -> dict:
    out = {k: v for k, v in current.items() if k in PRESERVE_ENV}
    out.update(ours)
    return out


def git_commit() -> str:
    try:
        c = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short=12", "HEAD"],
                           capture_output=True, text=True, timeout=5).stdout.strip()
        dirty = subprocess.run(["git", "-C", str(ROOT), "status", "--porcelain", "--untracked-files=no"],
                               capture_output=True, text=True, timeout=5).stdout.strip()
        return (c or "unknown") + ("-dirty" if dirty else "")
    except Exception:
        return "unknown"


def ensure_token(lam=None) -> str:
    token, src = resolve_token(TOKEN_FILE, existing_env(lam) if lam else {})
    print(f"API token: {src} (stored in {TOKEN_FILE.name}, gitignored; not printed)")
    return token


def build_zip():
    print("Building Lambda zip (manylinux py3.12 x86_64)…")
    if BUILD.exists():
        shutil.rmtree(BUILD)
    BUILD.mkdir(parents=True)
    if ZIP.exists():
        ZIP.unlink()
    subprocess.check_call([
        sys.executable, "-m", "pip", "install", "-q", "--target", str(BUILD),
        "--platform", "manylinux2014_x86_64", "--python-version", "3.12",
        "--only-binary=:all:",
        "fastapi", "mangum>=0.17", "python-multipart", "pymupdf", "Pillow", "PyYAML",
        "pydantic>=2.7,<3", "jsonschema[format-nongpl]>=4.23,<5", "referencing>=0.35,<1", "pydantic-core", "starlette", "anyio", "sniffio",
        "typing-extensions", "annotated-types",
    ])
    shutil.copytree(ROOT / "extractor", BUILD / "extractor",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "samples"))
    for p in BUILD.rglob("__pycache__"):
        shutil.rmtree(p, ignore_errors=True)
    with zipfile.ZipFile(ZIP, "w", zipfile.ZIP_DEFLATED) as z:
        for p in BUILD.rglob("*"):
            if p.is_file():
                z.write(p, p.relative_to(BUILD).as_posix())
    mb = ZIP.stat().st_size / 1e6
    print(f"Package {mb:.1f} MB zipped")
    return mb


def ensure_role(iam, account: str) -> str:
    trust = {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": "lambda.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }],
    }
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "Logs",
                "Effect": "Allow",
                "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
                "Resource": f"arn:aws:logs:{REGION}:{account}:*",
            },
            {
                "Sid": "Textract",
                "Effect": "Allow",
                "Action": ["textract:AnalyzeDocument", "textract:DetectDocumentText"],
                "Resource": "*",
            },
            {
                "Sid": "Bedrock",
                "Effect": "Allow",
                "Action": [
                    "bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream",
                    "bedrock:Converse", "bedrock:ConverseStream",
                ],
                "Resource": [
                    f"arn:aws:bedrock:{REGION}:{account}:inference-profile/*",
                    "arn:aws:bedrock:*::foundation-model/anthropic.*",
                    "arn:aws:bedrock:*::foundation-model/amazon.nova-*",
                ],
            },
        ],
    }
    try:
        role = iam.get_role(RoleName=ROLE_NAME)["Role"]
        print(f"Role exists {ROLE_NAME}")
    except ClientError:
        print(f"Creating role {ROLE_NAME}")
        role = iam.create_role(
            RoleName=ROLE_NAME,
            AssumeRolePolicyDocument=json.dumps(trust),
            Tags=TAGS,
            Description="KB extractor Lambda - least privilege Textract/Bedrock/Logs",
        )["Role"]
        time.sleep(12)
    iam.put_role_policy(
        RoleName=ROLE_NAME,
        PolicyName=f"{NAME}-inline",
        PolicyDocument=json.dumps(policy),
    )
    try:
        iam.tag_role(RoleName=ROLE_NAME, Tags=TAGS)
    except ClientError:
        pass
    return role["Arn"]


def upload_code(lam, s3, account: str, zip_mb: float):
    """Return kwargs for Code= on create, or update existing function code."""
    exists = True
    try:
        lam.get_function(FunctionName=NAME)
    except ClientError:
        exists = False

    env = merge_env(existing_env(lam) if exists else {}, {
        "EXTRACTOR_API_KEY": TOKEN_FILE.read_text().strip(),
        "AWS_REGION_EXTRACTOR": REGION,
        "BEDROCK_MODEL": "eu.anthropic.claude-sonnet-4-5-20250929-v1:0",
        "BEDROCK_CLASSIFY_MODEL": "eu.anthropic.claude-haiku-4-5-20251001-v1:0",
        "EXTRACTOR_BUDGET_USD": "2.5",
        "EXTRACTOR_GIT_COMMIT": git_commit(),      # audit records the exact code in Lambda
    })
    missing = [k for k in ("DEMO_INVOKE_ROLE_ARN", "FUNCTION_URL") if exists and k not in env]
    if missing:
        print(f"WARN existing function has no {missing}: /session will answer 500 until set")
    role_arn = None  # filled by caller

    def wait_updated():
        waiter = lam.get_waiter("function_updated")
        waiter.wait(FunctionName=NAME)

    if zip_mb < 50:
        code_kwargs = {"ZipFile": ZIP.read_bytes()}
        s3_cleanup = None
    else:
        bucket = f"{NAME}-pkg-{account}"
        key = f"package-{int(time.time())}.zip"
        print(f"Zip >50MB — temporary S3 {bucket}/{key} then delete")
        try:
            s3.head_bucket(Bucket=bucket)
        except ClientError:
            s3.create_bucket(
                Bucket=bucket,
                CreateBucketConfiguration={"LocationConstraint": REGION},
                ObjectOwnership="BucketOwnerEnforced",
            )
            s3.put_bucket_tagging(Bucket=bucket, Tagging={"TagSet": TAGS})
        s3.upload_file(str(ZIP), bucket, key)
        code_kwargs = {"S3Bucket": bucket, "S3Key": key}
        s3_cleanup = (bucket, key)

    return exists, code_kwargs, env, s3_cleanup


def create_or_update(lam, s3, account, role_arn, zip_mb):
    exists, code_kwargs, env, s3_cleanup = upload_code(lam, s3, account, zip_mb)
    if exists:
        print("Updating function code…")
        if "ZipFile" in code_kwargs:
            lam.update_function_code(FunctionName=NAME, ZipFile=code_kwargs["ZipFile"])
        else:
            lam.update_function_code(
                FunctionName=NAME, S3Bucket=code_kwargs["S3Bucket"], S3Key=code_kwargs["S3Key"])
        lam.get_waiter("function_updated").wait(FunctionName=NAME)
        print("Updating configuration…")
        lam.update_function_configuration(
            FunctionName=NAME,
            Runtime="python3.12",
            Role=role_arn,
            Handler="extractor.lambda_handler.handler",
            Timeout=180,
            MemorySize=1024,
            Environment={"Variables": env},
        )
        lam.get_waiter("function_updated").wait(FunctionName=NAME)
    else:
        print("Creating function…")
        kwargs = dict(
            FunctionName=NAME,
            Runtime="python3.12",
            Role=role_arn,
            Handler="extractor.lambda_handler.handler",
            Timeout=180,
            MemorySize=1024,
            Environment={"Variables": env},
            Tags=TAG_MAP,
            Code=code_kwargs,
            Description="FinTechers KB back-office document extractor",
        )
        lam.create_function(**kwargs)
        lam.get_waiter("function_active").wait(FunctionName=NAME)

    if s3_cleanup:
        bucket, key = s3_cleanup
        try:
            s3.delete_object(Bucket=bucket, Key=key)
            # empty + delete
            for obj in s3.list_objects_v2(Bucket=bucket).get("Contents") or []:
                s3.delete_object(Bucket=bucket, Key=obj["Key"])
            s3.delete_bucket(Bucket=bucket)
            print("Temporary S3 packaging bucket deleted")
        except ClientError as e:
            print(f"WARN S3 cleanup: {e}")

    try:
        lam.tag_resource(
            Resource=f"arn:aws:lambda:{REGION}:{account}:function:{NAME}", Tags=TAG_MAP)
    except ClientError:
        pass


def configure_ops(lam, logs):
    print("Reserved concurrency (cap abuse)")
    try:
        lam.put_function_concurrency(FunctionName=NAME, ReservedConcurrentExecutions=2)
        print("  set to 2")
    except ClientError as e:
        # Hackathon accounts often cannot drop UnreservedConcurrentExecution below 10.
        print(f"  WARN cannot set reserved=2 ({e.response['Error']['Code']}: {e.response['Error']['Message'][:120]})")
        try:
            lam.put_function_concurrency(FunctionName=NAME, ReservedConcurrentExecutions=1)
            print("  set to 1 (fallback)")
        except ClientError as e2:
            print(f"  WARN concurrency left unset: {e2.response['Error']['Message'][:120]}")
    lg = f"/aws/lambda/{NAME}"
    try:
        logs.create_log_group(logGroupName=lg, tags=TAG_MAP)
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceAlreadyExistsException":
            print(f"WARN create log group: {e}")
    logs.put_retention_policy(logGroupName=lg, retentionInDays=7)
    try:
        logs.tag_log_group(logGroupName=lg, tags=TAG_MAP)
    except ClientError:
        try:
            logs.tag_resource(
                resourceArn=f"arn:aws:logs:{REGION}:{boto3.client('sts', region_name=REGION).get_caller_identity()['Account']}:log-group:{lg}",
                tags=TAG_MAP,
            )
        except ClientError:
            pass

    print("Function URL (AuthType AWS_IAM; public access via CloudFront OAC)")
    try:
        lam.get_function_url_config(FunctionName=NAME)
    except ClientError:
        lam.create_function_url_config(
            FunctionName=NAME,
            AuthType="AWS_IAM",
            Cors={
                "AllowOrigins": ["*"],
                "AllowMethods": ["GET", "POST"],
                "AllowHeaders": ["content-type", "x-api-key"],
                "AllowCredentials": False,
            },
        )
        try:
            lam.add_permission(
                FunctionName=NAME,
                StatementId="FunctionURLAllowPublicAccess",
                Action="lambda:InvokeFunctionUrl",
                Principal="*",
                FunctionUrlAuthType="AWS_IAM",
            )
        except ClientError:
            pass
    url = lam.get_function_url_config(FunctionName=NAME)["FunctionUrl"]
    URL_FILE.write_text(url + "\n")
    return url


def main():
    session, account = clients()
    iam = session.client("iam")
    lam = session.client("lambda")
    ensure_token(lam)
    zip_mb = build_zip()
    s3 = session.client("s3")
    logs = session.client("logs")
    role_arn = ensure_role(iam, account)
    create_or_update(lam, s3, account, role_arn, zip_mb)
    url = configure_ops(lam, logs)
    print()
    print("Deployed.")
    print(f"Function: {NAME}")
    print(f"Role: {ROLE_NAME}")
    print(f"URL: {url}")
    print(f"Token file: {TOKEN_FILE}")
    print(f"Test: curl -s {url}health")


if __name__ == "__main__":
    main()
