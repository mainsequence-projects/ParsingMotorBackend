#!/usr/bin/env python3
"""Tear down CloudFront + OAC + Lambda + role + log group."""
import os, sys, time
import boto3
from botocore.exceptions import ClientError

REGION = "eu-central-1"
NAME = "fintechers-kb-extractor"
ROLE_NAME = f"{NAME}-role"
OAC_NAME = f"{NAME}-oac"
DIST_COMMENT = "fintechers-kb extractor public demo"

def main():
    os.environ["AWS_REGION"] = REGION
    session = boto3.Session(region_name=REGION)
    try:
        account = session.client("sts").get_caller_identity()["Account"]
    except ClientError as e:
        if "Expired" in e.response["Error"]["Code"]:
            print("ERROR: ExpiredToken", file=sys.stderr); sys.exit(1)
        raise
    lam, logs, iam, s3, cf = (session.client(x) for x in ("lambda", "logs", "iam", "s3", "cloudfront"))

    # CloudFront first
    dist_id = None
    marker = None
    while True:
        kwargs = {"Marker": marker} if marker else {}
        page = cf.list_distributions(**kwargs)
        items = (page.get("DistributionList") or {}).get("Items") or []
        for d in items:
            if d.get("Comment") == DIST_COMMENT:
                dist_id = d["Id"]; break
        if dist_id or not (page.get("DistributionList") or {}).get("IsTruncated"):
            break
        marker = page["DistributionList"].get("NextMarker")
    if dist_id:
        print("Disable + delete distribution", dist_id)
        cfg = cf.get_distribution_config(Id=dist_id)
        conf, etag = cfg["DistributionConfig"], cfg["ETag"]
        if conf["Enabled"]:
            conf["Enabled"] = False
            cf.update_distribution(Id=dist_id, IfMatch=etag, DistributionConfig=conf)
            while cf.get_distribution(Id=dist_id)["Distribution"]["Status"] != "Deployed":
                time.sleep(20)
            etag = cf.get_distribution_config(Id=dist_id)["ETag"]
        try:
            cf.delete_distribution(Id=dist_id, IfMatch=etag)
            print("Deleted distribution")
        except ClientError as e:
            print("delete dist", e)
    # OAC
    for oac in cf.list_origin_access_controls().get("OriginAccessControlList", {}).get("Items", []) or []:
        if oac.get("Name") == OAC_NAME:
            try:
                e = cf.get_origin_access_control(Id=oac["Id"])["ETag"]
                cf.delete_origin_access_control(Id=oac["Id"], IfMatch=e)
                print("Deleted OAC")
            except ClientError as e:
                print("OAC", e)

    # demo invoke role
    try:
        iam.delete_role_policy(RoleName=f"{NAME}-demo-invoke", PolicyName=f"{NAME}-demo-invoke-inline")
        iam.delete_role(RoleName=f"{NAME}-demo-invoke")
        print("Deleted demo-invoke role")
    except ClientError as e:
        print("demo-invoke", e.response["Error"]["Code"])
    # custom ORP
    try:
        for it in (cf.list_origin_request_policies(Type="custom").get("OriginRequestPolicyList") or {}).get("Items") or []:
            pol = it.get("OriginRequestPolicy", it)
            cfg = pol.get("OriginRequestPolicyConfig", {})
            if cfg.get("Name") == f"{NAME}-orp":
                et = cf.get_origin_request_policy(Id=pol["Id"])["ETag"]
                cf.delete_origin_request_policy(Id=pol["Id"], IfMatch=et)
                print("Deleted ORP")
    except ClientError as e:
        print("ORP", e)

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
