#!/usr/bin/env python3
"""Public CloudFront in front of IAM-auth Lambda Function URL (OAC).

Needed because the hackathon org blocks AuthType NONE (anonymous 403).
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import boto3
from botocore.exceptions import ClientError

REGION = "eu-central-1"
NAME = "fintechers-kb-extractor"
OAC_NAME = f"{NAME}-oac"
DIST_COMMENT = "fintechers-kb extractor public demo"
TAGS = [{"Key": "project", "Value": "fintechers-kb"}, {"Key": "owner", "Value": "carlos"}]
ROOT = Path(__file__).resolve().parents[1]
URL_FILE = ROOT / "deploy" / "function_url.txt"
PUBLIC_URL_FILE = ROOT / "deploy" / "public_url.txt"


def main():
    os.environ["AWS_REGION"] = REGION
    session = boto3.Session(region_name=REGION)
    try:
        account = session.client("sts").get_caller_identity()["Account"]
    except ClientError as e:
        if "Expired" in e.response["Error"]["Code"]:
            print("ERROR: ExpiredToken", file=sys.stderr)
            sys.exit(1)
        raise
    lam = session.client("lambda")
    cf = session.client("cloudfront")

    # Ensure Function URL is AWS_IAM
    try:
        cfg = lam.get_function_url_config(FunctionName=NAME)
    except ClientError:
        print("ERROR: function URL missing — run deploy.py first", file=sys.stderr)
        sys.exit(1)
    if cfg["AuthType"] != "AWS_IAM":
        lam.update_function_url_config(FunctionName=NAME, AuthType="AWS_IAM")
        print("Switched Function URL to AWS_IAM")

    origin_domain = urlparse(cfg["FunctionUrl"]).hostname
    print("Origin", origin_domain)

    # Create or reuse OAC
    oac_id = None
    for oac in cf.list_origin_access_controls().get("OriginAccessControlList", {}).get("Items", []) or []:
        if oac.get("Name") == OAC_NAME:
            oac_id = oac["Id"]
            break
    if not oac_id:
        resp = cf.create_origin_access_control(
            OriginAccessControlConfig={
                "Name": OAC_NAME,
                "Description": "OAC for KB extractor Lambda URL",
                "SigningProtocol": "sigv4",
                "SigningBehavior": "always",
                "OriginAccessControlOriginType": "lambda",
            }
        )
        oac_id = resp["OriginAccessControl"]["Id"]
        print("Created OAC", oac_id)
    else:
        print("Reusing OAC", oac_id)

    # Find existing distribution by comment/tag
    dist_id = None
    dist_domain = None
    marker = None
    while True:
        kwargs = {"Marker": marker} if marker else {}
        page = cf.list_distributions(**kwargs)
        items = (page.get("DistributionList") or {}).get("Items") or []
        for d in items:
            if d.get("Comment") == DIST_COMMENT:
                dist_id = d["Id"]
                dist_domain = d["DomainName"]
                break
        if dist_id or not (page.get("DistributionList") or {}).get("IsTruncated"):
            break
        marker = page["DistributionList"].get("NextMarker")

    cache_policy_id = "4135ea2d-6df8-44a3-9df3-4b5a84be39ad"  # Managed-CachingDisabled
    origin_req_policy = "b689b0a8-53d0-40ab-baf2-68738e2966ac"  # Managed-AllViewerExceptHostHeader

    if not dist_id:
        print("Creating CloudFront distribution (5–15 min)…")
        config = {
            "CallerReference": f"{NAME}-{int(time.time())}",
            "Comment": DIST_COMMENT,
            "Enabled": True,
            "DefaultRootObject": "",
            "Origins": {
                "Quantity": 1,
                "Items": [{
                    "Id": "lambda-url",
                    "DomainName": origin_domain,
                    "CustomOriginConfig": {
                        "HTTPPort": 80,
                        "HTTPSPort": 443,
                        "OriginProtocolPolicy": "https-only",
                        "OriginSslProtocols": {"Quantity": 1, "Items": ["TLSv1.2"]},
                        "OriginReadTimeout": 60,  # max without quota bump; packages may need retry
                        "OriginKeepaliveTimeout": 5,
                    },
                    "OriginAccessControlId": oac_id,
                    "ConnectionAttempts": 3,
                    "ConnectionTimeout": 10,
                }],
            },
            "DefaultCacheBehavior": {
                "TargetOriginId": "lambda-url",
                "ViewerProtocolPolicy": "redirect-to-https",
                "AllowedMethods": {
                    "Quantity": 7,
                    "Items": ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"],
                    "CachedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"]},
                },
                "Compress": True,
                "CachePolicyId": cache_policy_id,
                "OriginRequestPolicyId": origin_req_policy,
                "SmoothStreaming": False,
                "FieldLevelEncryptionId": "",
            },
            "PriceClass": "PriceClass_100",
            "HttpVersion": "http2",
            "IsIPV6Enabled": True,
        }
        resp = cf.create_distribution(DistributionConfig=config)
        dist_id = resp["Distribution"]["Id"]
        dist_domain = resp["Distribution"]["DomainName"]
        # tags
        try:
            cf.tag_resource(
                Resource=f"arn:aws:cloudfront::{account}:distribution/{dist_id}",
                Tags={"Items": TAGS},
            )
        except ClientError as e:
            print("WARN tag dist", e)
        print("Created distribution", dist_id, dist_domain)
    else:
        print("Reusing distribution", dist_id, dist_domain)
        # Ensure OAC attached
        d = cf.get_distribution_config(Id=dist_id)
        conf = d["DistributionConfig"]
        etag = d["ETag"]
        origin = conf["Origins"]["Items"][0]
        if origin.get("OriginAccessControlId") != oac_id:
            origin["OriginAccessControlId"] = oac_id
            # remove legacy OAI if any
            origin.pop("S3OriginConfig", None)
            cf.update_distribution(Id=dist_id, IfMatch=etag, DistributionConfig=conf)
            print("Updated origin OAC")

    # Permissions for CloudFront service principal
    source_arn = f"arn:aws:cloudfront::{account}:distribution/{dist_id}"
    for sid, action in (
        ("AllowCloudFrontServicePrincipal", "lambda:InvokeFunctionUrl"),
        ("AllowCloudFrontServicePrincipalInvokeFunction", "lambda:InvokeFunction"),
    ):
        try:
            lam.remove_permission(FunctionName=NAME, StatementId=sid)
        except ClientError:
            pass
        try:
            lam.add_permission(
                FunctionName=NAME,
                StatementId=sid,
                Action=action,
                Principal="cloudfront.amazonaws.com",
                SourceArn=source_arn,
            )
            print("Granted", action)
        except ClientError as e:
            print("WARN permission", action, e)

    # Remove public NONE permission if present
    for sid in ("FunctionURLAllowPublicAccess", "FunctionURLAllowAccount"):
        try:
            lam.remove_permission(FunctionName=NAME, StatementId=sid)
        except ClientError:
            pass

    public = f"https://{dist_domain}/"
    PUBLIC_URL_FILE.write_text(public + "\n")
    URL_FILE.write_text(cfg["FunctionUrl"] + "\n")

    # Wait until Deployed
    print("Waiting for CloudFront status Deployed…")
    while True:
        st = cf.get_distribution(Id=dist_id)["Distribution"]["Status"]
        print(" ", st)
        if st == "Deployed":
            break
        time.sleep(30)

    print()
    print("PUBLIC_URL", public)
    print("LAMBDA_URL", cfg["FunctionUrl"], "(AWS_IAM only)")
    print("DIST_ID", dist_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
