#!/usr/bin/env python3
"""CLI helper: session via CloudFront + SigV4 POST to Lambda Function URL."""
import argparse, json, sys, urllib.request
from pathlib import Path
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials

ROOT = Path(__file__).resolve().parents[1]


def signed_post(operation: str, body: dict, public_url: str, token_file=ROOT / ".deploy_token") -> dict:
    """GET /session through CloudFront, then SigV4-sign the POST to the Function URL."""
    token = Path(token_file).read_text().strip()
    pub = public_url if public_url.endswith("/") else public_url + "/"
    req = urllib.request.Request(pub + "session", headers={"x-api-key": token})
    with urllib.request.urlopen(req, timeout=30) as resp:
        s = json.loads(resp.read())
    data = json.dumps(body).encode()
    url = s["functionUrl"].rstrip("/") + "/" + operation
    creds = Credentials(s["accessKeyId"], s["secretAccessKey"], s["sessionToken"])
    headers = {"content-type": "application/json", "x-api-key": token, "Host": url.split("/")[2]}
    areq = AWSRequest(method="POST", url=url, data=data, headers=headers)
    SigV4Auth(creds, "lambda", s["region"]).add_auth(areq)
    prepared = areq.prepare()
    ur = urllib.request.Request(prepared.url, data=data, headers=dict(prepared.headers), method="POST")
    with urllib.request.urlopen(ur, timeout=180) as resp:
        return json.loads(resp.read())


def main():
    p = argparse.ArgumentParser()
    p.add_argument("operation", choices=["classify_json", "extract_typed_json"])
    p.add_argument("request", type=Path, help="Full JSON request including catalog or schemas")
    p.add_argument("--public-url", required=True)
    p.add_argument("--token-file", default=str(ROOT / ".deploy_token"))
    args = p.parse_args()
    body = json.loads(args.request.read_text())
    sys.stdout.write(json.dumps(signed_post(args.operation, body, args.public_url, args.token_file)))


if __name__ == "__main__":
    main()
