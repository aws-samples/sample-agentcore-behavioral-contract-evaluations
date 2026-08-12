#!/usr/bin/env python3
"""Pre-flight checks for the sample walkthrough.

Verifies -- before you spend ten minutes on a deploy -- that:

  1. AWS credentials resolve to an account
  2. Amazon Bedrock AgentCore is available in the target region
  3. The Bedrock model the demo agent uses is accessible
     (issues a 1-token test request, costs well under $0.01)
  4. The account/region is CDK-bootstrapped (first `agentcore deploy` needs it)
  5. Node.js 20+ and the AgentCore CLI (>= 0.21) are installed

Usage:
    python scripts/preflight.py [--region us-west-2]

Exit code 0 = ready to deploy; 1 = at least one check failed.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys

import boto3
from botocore.exceptions import BotoCoreError, ClientError, EndpointConnectionError, NoCredentialsError

# Keep in sync with claimsdemo/app/claims_agent/model/load.py
MODEL_ID = "global.anthropic.claude-sonnet-5"

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"


def _result(status: str, name: str, detail: str, fix: str = "") -> dict:
    return {"status": status, "name": name, "detail": detail, "fix": fix}


def check_credentials(session: boto3.Session) -> dict:
    name = "AWS credentials"
    try:
        ident = session.client("sts").get_caller_identity()
        return _result(PASS, name, f"account {ident['Account']} ({ident['Arn']})")
    except (NoCredentialsError, ClientError, BotoCoreError) as exc:
        return _result(FAIL, name, str(exc),
                       "Configure credentials: `aws configure` or AWS_PROFILE / environment variables.")


def check_agentcore_region(session: boto3.Session, region: str) -> dict:
    name = "AgentCore region"
    try:
        ctl = session.client("bedrock-agentcore-control")
        ctl.list_agent_runtimes(maxResults=1)
        return _result(PASS, name, region)
    except EndpointConnectionError:
        return _result(FAIL, name, f"no AgentCore endpoint in {region}",
                       "Pick a supported region: "
                       "https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agentcore-regions.html")
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("AccessDeniedException", "UnauthorizedException"):
            return _result(WARN, name, f"{region} reachable, but caller lacks bedrock-agentcore permissions",
                           "The walkthrough needs AgentCore control-plane access; check your IAM policy.")
        return _result(FAIL, name, f"{code}: {exc}")
    except Exception as exc:  # noqa: BLE001 -- includes UnknownServiceError from old boto3
        return _result(FAIL, name, str(exc),
                       "If this is UnknownServiceError, upgrade boto3: `pip install -U boto3`.")


def check_model_access(session: boto3.Session) -> dict:
    name = "Bedrock model access"
    try:
        rt = session.client("bedrock-runtime")
        rt.converse(
            modelId=MODEL_ID,
            messages=[{"role": "user", "content": [{"text": "ping"}]}],
            inferenceConfig={"maxTokens": 1},
        )
        return _result(PASS, name, MODEL_ID)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code == "ThrottlingException":
            return _result(PASS, name, f"{MODEL_ID} (throttled -- access confirmed)")
        if code in ("AccessDeniedException", "ResourceNotFoundException", "ValidationException"):
            return _result(FAIL, name, f"{code} invoking {MODEL_ID}",
                           "Enable Anthropic Claude Sonnet 5 in the Bedrock console -> Model access, "
                           "in this region. Deploy succeeds without it, but every agent invocation will fail.")
        return _result(FAIL, name, f"{code}: {exc}")
    except (BotoCoreError, EndpointConnectionError) as exc:
        return _result(FAIL, name, str(exc))


def check_cdk_bootstrap(session: boto3.Session, region: str) -> dict:
    name = "CDK bootstrap"
    try:
        cfn = session.client("cloudformation")
        cfn.describe_stacks(StackName="CDKToolkit")
        return _result(PASS, name, "CDKToolkit stack present")
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code == "ValidationError":  # stack does not exist
            account = "<ACCOUNT>"
            try:
                account = session.client("sts").get_caller_identity()["Account"]
            except Exception:  # noqa: BLE001
                pass
            return _result(FAIL, name, "account/region not bootstrapped",
                           f"One-time setup: `npx cdk bootstrap aws://{account}/{region}` "
                           "(the first `agentcore deploy` requires it).")
        return _result(WARN, name, f"could not verify ({code})")
    except (BotoCoreError, EndpointConnectionError) as exc:
        return _result(WARN, name, f"could not verify ({exc})")


def _cli_version(cmd: list[str]) -> str:
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    return (out.stdout or out.stderr).strip().splitlines()[0] if (out.stdout or out.stderr) else ""


def check_node() -> dict:
    name = "Node.js 20+"
    if not shutil.which("node"):
        return _result(FAIL, name, "node not found on PATH",
                       "Install Node.js 20 or later: https://nodejs.org/")
    version = _cli_version(["node", "--version"])
    match = re.match(r"v(\d+)", version)
    if match and int(match.group(1)) >= 20:
        return _result(PASS, name, version)
    return _result(FAIL, name, f"found {version or 'unknown'}",
                   "The AgentCore CLI needs Node.js 20+.")


def check_agentcore_cli() -> dict:
    name = "AgentCore CLI >= 0.21"
    if not shutil.which("agentcore"):
        return _result(FAIL, name, "agentcore not found on PATH",
                       "Install it: `npm install -g @aws/agentcore`")
    version = _cli_version(["agentcore", "--version"])
    match = re.search(r"(\d+)\.(\d+)", version)
    if match and (int(match.group(1)), int(match.group(2))) >= (0, 21):
        return _result(PASS, name, version)
    return _result(FAIL, name, f"found {version or 'unknown'}",
                   "Upgrade: `npm install -g @aws/agentcore@latest`")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    args = parser.parse_args()

    session = boto3.Session(region_name=args.region)
    print(f"Pre-flight checks (region: {args.region})\n")

    results = [check_credentials(session)]
    if results[0]["status"] == PASS:
        results += [
            check_agentcore_region(session, args.region),
            check_model_access(session),
            check_cdk_bootstrap(session, args.region),
        ]
    results += [check_node(), check_agentcore_cli()]

    for r in results:
        print(f"  [{r['status']}] {r['name']:<24} {r['detail']}")
        if r["fix"]:
            print(f"         fix: {r['fix']}")

    failed = [r for r in results if r["status"] == FAIL]
    print()
    if failed:
        print(f"{len(failed)} check(s) failed -- fix the items above before `agentcore deploy`.")
        return 1
    print("All checks passed. Continue with step 1 of the quickstart.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
