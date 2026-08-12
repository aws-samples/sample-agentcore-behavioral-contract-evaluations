#!/usr/bin/env python3
"""Build and deploy the contract evaluator Lambda.

Builds a deployment zip (handler + span shim + contract YAML + vendored
libraries), creates the IAM role and optional audit S3 bucket, and
creates/updates the Lambda function.

Usage:
    python scripts/deploy_evaluator_lambda.py [--function-name claims-contract-evaluator]
        [--audit-bucket-prefix agentcore-contract-audit] [--no-audit] [--region us-west-2]

Dependency resolution: the contract engine and OTEL normalization layer are
vendored in evaluator/vendor/ and copied into the package root; the only
pip-installed dependency is pyyaml.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import boto3

SAMPLE_ROOT = Path(__file__).parent.parent
EVALUATOR_DIR = SAMPLE_ROOT / "evaluator"
BUILD_DIR = EVALUATOR_DIR / "build"


def build_zip() -> bytes:
    """Assemble the Lambda deployment package."""
    if BUILD_DIR.exists():
        shutil.rmtree(BUILD_DIR)
    BUILD_DIR.mkdir(parents=True)

    print("Installing dependencies (pyyaml)...")
    cmd = [
        sys.executable, "-m", "pip", "install",
        "--target", str(BUILD_DIR),
        "--quiet",
        "--platform", "manylinux2014_x86_64",
        "--implementation", "cp",
        "--only-binary", ":all:",
        "-r", str(EVALUATOR_DIR / "requirements.txt"),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        # Pure-python fallback without platform pinning.
        cmd = [sys.executable, "-m", "pip", "install", "--target", str(BUILD_DIR), "--quiet",
               "-r", str(EVALUATOR_DIR / "requirements.txt")]
        subprocess.run(cmd, check=True)

    # Vendored libraries go to the package root so imports resolve natively.
    vendor_dir = EVALUATOR_DIR / "vendor"
    for pkg in ("agent_validator", "agentic_otel"):
        shutil.copytree(
            vendor_dir / pkg, BUILD_DIR / pkg,
            ignore=shutil.ignore_patterns("__pycache__"),
        )
    shutil.copy(vendor_dir / "LICENSE", BUILD_DIR / "VENDOR_LICENSE")

    # Copy evaluator sources
    shutil.copy(EVALUATOR_DIR / "handler.py", BUILD_DIR / "handler.py")
    shutil.copy(EVALUATOR_DIR / "span_shim.py", BUILD_DIR / "span_shim.py")
    shutil.copytree(EVALUATOR_DIR / "contracts", BUILD_DIR / "contracts")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted(BUILD_DIR.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                z.write(path, path.relative_to(BUILD_DIR))
    buf.seek(0)
    data = buf.read()
    print(f"Deployment package: {len(data) / 1e6:.1f} MB")
    return data


def ensure_role(iam, role_name: str, audit_bucket: str | None) -> str:
    trust = {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": "lambda.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }],
    }
    created = False
    try:
        role_arn = iam.get_role(RoleName=role_name)["Role"]["Arn"]
    except iam.exceptions.NoSuchEntityException:
        role_arn = iam.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=json.dumps(trust),
            Description="Execution role for the AgentCore contract evaluator Lambda",
        )["Role"]["Arn"]
        iam.attach_role_policy(
            RoleName=role_name,
            PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
        )
        created = True

    if audit_bucket:
        iam.put_role_policy(
            RoleName=role_name,
            PolicyName="audit-bucket-write",
            PolicyDocument=json.dumps({
                "Version": "2012-10-17",
                "Statement": [{
                    "Effect": "Allow",
                    "Action": "s3:PutObject",
                    "Resource": [
                        f"arn:aws:s3:::{audit_bucket}/audit/*",
                        f"arn:aws:s3:::{audit_bucket}/debug/*",
                    ],
                }],
            }),
        )
    if created:
        print("IAM role created; waiting for propagation...")
        time.sleep(10)
    return role_arn


def ensure_bucket(s3, bucket: str, region: str) -> None:
    try:
        s3.head_bucket(Bucket=bucket)
    except Exception:
        kwargs = {"Bucket": bucket}
        if region != "us-east-1":
            kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region}
        s3.create_bucket(**kwargs)
        s3.put_public_access_block(
            Bucket=bucket,
            PublicAccessBlockConfiguration={
                "BlockPublicAcls": True,
                "IgnorePublicAcls": True,
                "BlockPublicPolicy": True,
                "RestrictPublicBuckets": True,
            },
        )
        print(f"Created audit bucket s3://{bucket}")


def deploy_function(lam, name: str, role_arn: str, zip_bytes: bytes, env: dict) -> str:
    config = dict(
        Runtime="python3.12",
        Role=role_arn,
        Handler="handler.handler",
        Timeout=60,
        MemorySize=512,
        Environment={"Variables": env},
        Description="AgentCore Evaluations code-based evaluator: behavioral contract validation",
    )
    try:
        lam.get_function(FunctionName=name)
        lam.update_function_code(FunctionName=name, ZipFile=zip_bytes)
        waiter = lam.get_waiter("function_updated_v2")
        waiter.wait(FunctionName=name)
        resp = lam.update_function_configuration(FunctionName=name, **config)
        waiter.wait(FunctionName=name)
        arn = resp["FunctionArn"]
        print(f"Updated function {arn}")
    except lam.exceptions.ResourceNotFoundException:
        for _ in range(6):
            try:
                resp = lam.create_function(FunctionName=name, Code={"ZipFile": zip_bytes}, **config)
                break
            except lam.exceptions.InvalidParameterValueException as e:
                if "assume" in str(e).lower():
                    time.sleep(10)
                else:
                    raise
        arn = resp["FunctionArn"]
        lam.get_waiter("function_active_v2").wait(FunctionName=name)
        print(f"Created function {arn}")
    return arn


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--function-name", default="claims-contract-evaluator")
    parser.add_argument("--role-name", default="claims-contract-evaluator-role")
    parser.add_argument("--audit-bucket-prefix", default="agentcore-contract-audit")
    parser.add_argument("--no-audit", action="store_true", help="Skip the S3 audit bucket")
    parser.add_argument("--debug-capture", action="store_true",
                        help="Also write each raw evaluator payload to s3://<audit-bucket>/debug/ "
                             "(for capturing test fixtures; turn off by redeploying without this flag)")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    args = parser.parse_args()

    session = boto3.Session(region_name=args.region)
    account = session.client("sts").get_caller_identity()["Account"]
    iam = session.client("iam")
    lam = session.client("lambda")
    s3 = session.client("s3")

    audit_bucket = None
    if not args.no_audit:
        audit_bucket = f"{args.audit_bucket_prefix}-{account}-{args.region}"
        ensure_bucket(s3, audit_bucket, args.region)

    role_arn = ensure_role(iam, args.role_name, audit_bucket)
    zip_bytes = build_zip()

    env = {"CONTRACTS_DIR": "contracts"}
    if audit_bucket:
        env["AUDIT_BUCKET"] = audit_bucket
    if args.debug_capture:
        if not audit_bucket:
            print("error: --debug-capture requires the audit bucket (remove --no-audit)", file=sys.stderr)
            return 1
        env["DEBUG_CAPTURE"] = "1"

    arn = deploy_function(lam, args.function_name, role_arn, zip_bytes, env)

    print("\nNext steps:")
    print(f"  cd claimsdemo && agentcore add evaluator --name claims_contract \\")
    print(f"    --level TRACE --type code-based --lambda-arn {arn} --timeout 60")
    print(f"  agentcore deploy --yes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
