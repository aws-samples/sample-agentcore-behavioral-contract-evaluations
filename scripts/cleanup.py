#!/usr/bin/env python3
"""Tear down the sample's AWS resources in dependency order.

Order matters:
1. Online eval config + evaluators + runtime are managed by the CDK stack --
   remove them from agentcore.json and run `agentcore deploy`, or delete the
   whole stack (default here).
2. The contract evaluator Lambda, its IAM role, and the audit bucket are
   managed by deploy_evaluator_lambda.py and removed here.

Usage:
    # 1. Delete the AgentCore CDK stack (runtime, evaluators, online config)
    aws cloudformation delete-stack --stack-name AgentCore-claimsdemo-default

    # 2. Delete the Lambda-side resources
    python scripts/cleanup.py [--region us-west-2] [--keep-audit-bucket]
"""

from __future__ import annotations

import argparse
import os
import sys

import boto3

FUNCTION_NAME = "claims-contract-evaluator"
ROLE_NAME = "claims-contract-evaluator-role"
BUCKET_PREFIX = "agentcore-contract-audit"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    parser.add_argument("--keep-audit-bucket", action="store_true")
    parser.add_argument("--yes", action="store_true", help="Skip confirmation prompt")
    args = parser.parse_args()

    session = boto3.Session(region_name=args.region)
    account = session.client("sts").get_caller_identity()["Account"]
    bucket = f"{BUCKET_PREFIX}-{account}-{args.region}"

    print("Will delete:")
    print(f"  Lambda function: {FUNCTION_NAME}")
    print(f"  IAM role:        {ROLE_NAME}")
    if not args.keep_audit_bucket:
        print(f"  S3 bucket:       {bucket} (and all audit records)")
    if not args.yes:
        if input("Proceed? [y/N] ").strip().lower() != "y":
            print("Aborted.")
            return 1

    lam = session.client("lambda")
    iam = session.client("iam")

    try:
        lam.delete_function(FunctionName=FUNCTION_NAME)
        print(f"Deleted function {FUNCTION_NAME}")
    except lam.exceptions.ResourceNotFoundException:
        print(f"Function {FUNCTION_NAME} not found (already deleted)")

    try:
        for policy in iam.list_attached_role_policies(RoleName=ROLE_NAME)["AttachedPolicies"]:
            iam.detach_role_policy(RoleName=ROLE_NAME, PolicyArn=policy["PolicyArn"])
        for name in iam.list_role_policies(RoleName=ROLE_NAME)["PolicyNames"]:
            iam.delete_role_policy(RoleName=ROLE_NAME, PolicyName=name)
        iam.delete_role(RoleName=ROLE_NAME)
        print(f"Deleted role {ROLE_NAME}")
    except iam.exceptions.NoSuchEntityException:
        print(f"Role {ROLE_NAME} not found (already deleted)")

    if not args.keep_audit_bucket:
        s3 = session.resource("s3")
        try:
            b = s3.Bucket(bucket)
            b.objects.all().delete()
            b.delete()
            print(f"Deleted bucket {bucket}")
        except Exception as exc:  # noqa: BLE001
            print(f"Bucket {bucket}: {exc}")

    print("\nReminder: the AgentCore stack (runtime, evaluators, online eval config)")
    print("is deleted separately:")
    print("  aws cloudformation delete-stack --stack-name AgentCore-claimsdemo-default")
    return 0


if __name__ == "__main__":
    sys.exit(main())
