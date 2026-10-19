#!/usr/bin/env python3
"""Tear down Lambda + role + log group."""
import os, sys
import boto3
from botocore.exceptions import ClientError

REGION = "eu-central-1"
NAME = "fintechers-kb-extractor"
ROLE_NAME = f"{NAME}-role"

def main():
    os.environ["AWS_REGION"] = REGION
    session = boto3.Session(region_name=REGION)
    try:
        account = session.client("sts").get_caller_identity()["Account"]
    except ClientError as e:
        if "Expired" in e.response["Error"]["Code"]:
            print("ERROR: ExpiredToken", file=sys.stderr); sys.exit(1)
        raise
    lam, logs, iam, s3 = (session.client(x) for x in ("lambda", "logs", "iam", "s3"))
    for fn in (
        lambda: lam.delete_function_url_config(FunctionName=NAME),
        lambda: lam.delete_function(FunctionName=NAME),
        lambda: logs.delete_log_group(logGroupName=f"/aws/lambda/{NAME}"),
        lambda: iam.delete_role_policy(RoleName=ROLE_NAME, PolicyName=f"{NAME}-inline"),
        lambda: iam.delete_role(RoleName=ROLE_NAME),
    ):
        try:
            fn()
        except ClientError as e:
            print("skip:", e.response["Error"]["Code"])
    bucket = f"{NAME}-pkg-{account}"
    try:
        for obj in s3.list_objects_v2(Bucket=bucket).get("Contents") or []:
            s3.delete_object(Bucket=bucket, Key=obj["Key"])
        s3.delete_bucket(Bucket=bucket)
    except ClientError:
        pass
    print("Teardown complete.")

if __name__ == "__main__":
    main()
