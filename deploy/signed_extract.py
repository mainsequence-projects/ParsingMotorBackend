#!/usr/bin/env python3
"""CLI helper: session via CloudFront + SigV4 POST to Lambda Function URL."""
import argparse, base64, json, sys, urllib.request
from pathlib import Path
import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials

ROOT = Path(__file__).resolve().parents[1]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("files", nargs="+")
    p.add_argument("--public-url", default=(ROOT / "deploy/public_url.txt").read_text().strip())
    p.add_argument("--token-file", default=str(ROOT / ".deploy_token"))
    args = p.parse_args()
    token = Path(args.token_file).read_text().strip()
    pub = args.public_url if args.public_url.endswith("/") else args.public_url + "/"
    req = urllib.request.Request(pub + "session", headers={"x-api-key": token})
    with urllib.request.urlopen(req, timeout=30) as resp:
        s = json.loads(resp.read())
    files = []
    for f in args.files:
        path = Path(f)
        files.append({"filename": path.name, "content_base64": base64.b64encode(path.read_bytes()).decode()})
    body = json.dumps({"files": files}).encode()
    url = s["functionUrl"] + "extract_json"
    creds = Credentials(s["accessKeyId"], s["secretAccessKey"], s["sessionToken"])
    headers = {"content-type": "application/json", "x-api-key": token, "Host": url.split("/")[2]}
    areq = AWSRequest(method="POST", url=url, data=body, headers=headers)
    SigV4Auth(creds, "lambda", s["region"]).add_auth(areq)
    prepared = areq.prepare()
    ur = urllib.request.Request(prepared.url, data=body, headers=dict(prepared.headers), method="POST")
    with urllib.request.urlopen(ur, timeout=180) as resp:
        sys.stdout.write(resp.read().decode())


if __name__ == "__main__":
    main()
