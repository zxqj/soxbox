#!/usr/bin/env python3
"""Launch an EC2 instance, open an SSH SOCKS tunnel to it, and browse via Firefox."""

import argparse
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import boto3
import yaml
from botocore.exceptions import (
    ClientError,
    NoCredentialsError,
    SSOTokenLoadError,
    TokenRetrievalError,
    UnauthorizedSSOTokenError,
)


# Searched when --config is not given. Highest precedence first.
CONFIG_SEARCH_PATHS = [
    os.path.expanduser("~/.config/soxbox.yaml"),
    "/usr/local/etc/soxbox.yaml",
    "/etc/soxbox.yaml",
]

DEFAULT_REGION = "us-east-2"
DEFAULT_KEY_NAME = "soxbox"
DEFAULT_SG_NAME = "soxbox"
# Canonical's AWS account, owner of official Ubuntu AMIs.
UBUNTU_OWNER_ID = "099720109477"


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def find_config(explicit: str | None) -> str | None:
    if explicit is not None:
        return explicit
    for path in CONFIG_SEARCH_PATHS:
        if os.path.exists(path):
            return path
    return None


def find_free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


_EXPIRED_CRED_ERROR_CODES = {
    "ExpiredToken",
    "ExpiredTokenException",
    "RequestExpired",
    "InvalidClientTokenId",
}


def _credentials_valid(session: boto3.Session) -> bool:
    """Probe the session with a free STS call. Treat expired or missing tokens as invalid."""
    try:
        session.client("sts").get_caller_identity()
        return True
    except (
        NoCredentialsError,
        SSOTokenLoadError,
        TokenRetrievalError,
        UnauthorizedSSOTokenError,
    ):
        return False
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in _EXPIRED_CRED_ERROR_CODES:
            return False
        raise


def refresh_sso_credentials(profile: str | None) -> bool:
    """Run `aws sso login --no-browser` so it prints a URL and waits for the user to authenticate."""
    if shutil.which("aws") is None:
        print("`aws` CLI not found; cannot refresh SSO credentials automatically.", file=sys.stderr)
        return False
    cmd = ["aws", "sso", "login", "--no-browser"]
    if profile:
        cmd += ["--profile", profile]
    print("AWS credentials are expired. Running `aws sso login`...", file=sys.stderr)
    try:
        return subprocess.run(cmd).returncode == 0
    except KeyboardInterrupt:
        return False


def get_session(region: str, profile: str | None) -> boto3.Session:
    """Build a boto3 session, transparently refreshing expired SSO credentials."""
    session = boto3.Session(region_name=region, profile_name=profile)
    if _credentials_valid(session):
        return session
    if not refresh_sso_credentials(profile):
        raise RuntimeError("could not refresh AWS credentials")
    session = boto3.Session(region_name=region, profile_name=profile)
    if not _credentials_valid(session):
        raise RuntimeError("AWS credentials still invalid after `aws sso login`")
    return session


def wait_for_ssh(host: str, port: int = 22, timeout: int = 240) -> None:
    print(f"Waiting for SSH on {host}:{port}...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=5):
                return
        except OSError:
            time.sleep(3)
    raise TimeoutError(f"SSH on {host}:{port} did not become reachable in {timeout}s")


def get_caller_public_ip() -> str:
    with urllib.request.urlopen("https://checkip.amazonaws.com", timeout=5) as r:
        return r.read().decode().strip()


def ensure_keypair(ec2, key_name: str) -> str:
    """Ensure the AWS keypair and matching ~/.ssh/{key_name}.pem both exist; return the pem path."""
    pem_path = os.path.expanduser(f"~/.ssh/{key_name}.pem")
    try:
        ec2.describe_key_pairs(KeyNames=[key_name])
        remote_exists = True
    except ClientError as e:
        if e.response["Error"]["Code"] != "InvalidKeyPair.NotFound":
            raise
        remote_exists = False
    local_exists = os.path.exists(pem_path)

    if remote_exists and local_exists:
        return pem_path
    if remote_exists != local_exists:
        raise RuntimeError(
            f"Keypair state mismatch: AWS keypair '{key_name}' "
            f"{'exists' if remote_exists else 'is missing'} but {pem_path} "
            f"{'exists' if local_exists else 'is missing'}. "
            "Remove the orphan and retry."
        )

    print(f"Creating AWS keypair '{key_name}'...")
    response = ec2.create_key_pair(KeyName=key_name, KeyType="rsa", KeyFormat="pem")
    os.makedirs(os.path.dirname(pem_path), exist_ok=True)
    fd = os.open(pem_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(response["KeyMaterial"])
    print(f"Saved private key to {pem_path}")
    return pem_path


def ensure_security_group(ec2, name: str) -> str:
    """Ensure a security group with the given name exists; create + open TCP/22 if needed."""
    try:
        ec2.describe_security_groups(GroupNames=[name])
        return name
    except ClientError as e:
        if e.response["Error"]["Code"] not in ("InvalidGroup.NotFound", "InvalidGroupId.NotFound"):
            raise

    print(f"Creating security group '{name}'...")
    ec2.create_security_group(GroupName=name, Description="soxbox SSH access")
    try:
        my_ip = get_caller_public_ip()
        ec2.authorize_security_group_ingress(
            GroupName=name,
            IpPermissions=[{
                "IpProtocol": "tcp",
                "FromPort": 22,
                "ToPort": 22,
                "IpRanges": [{"CidrIp": f"{my_ip}/32", "Description": "soxbox SSH"}],
            }],
        )
        print(f"Authorized TCP/22 from {my_ip}/32 in {name}")
    except Exception as e:
        print(f"Warning: failed to authorize SSH ingress in {name}: {e}", file=sys.stderr)
    return name


def find_default_ami(ec2) -> tuple[str, str]:
    """Return (ami_id, ssh_user) for latest Amazon Linux 2023 x86_64, falling back to Ubuntu LTS."""
    al = ec2.describe_images(
        Owners=["amazon"],
        Filters=[
            {"Name": "name", "Values": ["al2023-ami-*-x86_64"]},
            {"Name": "state", "Values": ["available"]},
            {"Name": "architecture", "Values": ["x86_64"]},
        ],
    ).get("Images", [])
    if al:
        latest = max(al, key=lambda i: i["CreationDate"])
        return latest["ImageId"], "ec2-user"

    ub = ec2.describe_images(
        Owners=[UBUNTU_OWNER_ID],
        Filters=[
            {"Name": "name", "Values": ["ubuntu/images/hvm-ssd*/ubuntu-*-amd64-server-*"]},
            {"Name": "state", "Values": ["available"]},
            {"Name": "architecture", "Values": ["x86_64"]},
        ],
    ).get("Images", [])
    if ub:
        latest = max(ub, key=lambda i: i["CreationDate"])
        return latest["ImageId"], "ubuntu"

    raise RuntimeError("No Amazon Linux or Ubuntu AMI found in this region.")


def find_default_instance_type(ec2) -> str:
    """Pick a *.nano then *.micro that's offered in this region; otherwise confirm a fallback with the user."""
    nano = ["t3.nano", "t3a.nano", "t2.nano"]
    micro = ["t3.micro", "t3a.micro", "t2.micro"]
    available = set()
    paginator = ec2.get_paginator("describe_instance_type_offerings")
    for page in paginator.paginate(LocationType="region"):
        for offering in page["InstanceTypeOfferings"]:
            available.add(offering["InstanceType"])
    for t in nano + micro:
        if t in available:
            return t

    suggestion = "t3.small"
    answer = input(
        f"No *.nano or *.micro instance type is available in this region. "
        f"Use '{suggestion}'? [y/N] "
    ).strip().lower()
    if answer in ("y", "yes"):
        return suggestion
    chosen = input("Enter instance type to use: ").strip()
    if not chosen:
        raise RuntimeError("No instance type provided")
    return chosen


def launch_instance(ec2, config: dict) -> str:
    print(f"Launching {config['instance_type']} from {config['ami_id']}...")
    response = ec2.run_instances(
        ImageId=config["ami_id"],
        InstanceType=config["instance_type"],
        KeyName=config["key_pair"],
        SecurityGroups=[config["security_group"]],
        MinCount=1,
        MaxCount=1,
        TagSpecifications=[{
            "ResourceType": "instance",
            "Tags": [{"Key": "Name", "Value": "soxbox"}],
        }],
    )
    instance_id = response["Instances"][0]["InstanceId"]
    print(f"Instance ID: {instance_id}")
    return instance_id


def wait_for_running(ec2, instance_id: str) -> str:
    print("Waiting for instance to enter 'running' state...")
    ec2.get_waiter("instance_running").wait(InstanceIds=[instance_id])
    info = ec2.describe_instances(InstanceIds=[instance_id])
    instance = info["Reservations"][0]["Instances"][0]
    host = instance.get("PublicDnsName") or instance.get("PublicIpAddress")
    if not host:
        raise RuntimeError("Instance has no public hostname or IP address")
    return host


def terminate_instance(ec2, instance_id: str) -> None:
    print(f"Terminating {instance_id}...")
    try:
        ec2.terminate_instances(InstanceIds=[instance_id])
    except ClientError as e:
        print(f"Warning: failed to terminate instance: {e}", file=sys.stderr)


def start_ssh_tunnel(host: str, identity_file: str, ssh_user: str, local_port: int) -> subprocess.Popen:
    cmd = [
        "ssh",
        "-i", identity_file,
        "-D", f"127.0.0.1:{local_port}",
        "-N",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ServerAliveInterval=30",
        "-o", "ExitOnForwardFailure=yes",
        f"{ssh_user}@{host}",
    ]
    print(f"Starting SSH SOCKS tunnel on 127.0.0.1:{local_port}...")
    return subprocess.Popen(cmd)


def make_firefox_profile(socks_port: int) -> str:
    profile_dir = tempfile.mkdtemp(prefix="soxbox-ff-")
    user_js = (
        'user_pref("network.proxy.type", 1);\n'
        'user_pref("network.proxy.socks", "127.0.0.1");\n'
        f'user_pref("network.proxy.socks_port", {socks_port});\n'
        'user_pref("network.proxy.socks_version", 5);\n'
        'user_pref("network.proxy.socks_remote_dns", true);\n'
        'user_pref("network.proxy.no_proxies_on", "");\n'
        'user_pref("browser.privatebrowsing.autostart", true);\n'
        'user_pref("browser.shell.checkDefaultBrowser", false);\n'
        'user_pref("browser.startup.homepage_override.mstone", "ignore");\n'
    )
    Path(profile_dir, "user.js").write_text(user_js)
    return profile_dir


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=None, help="Path to config file (overrides discovery)")
    args = parser.parse_args()

    config_path = find_config(args.config)
    if config_path is None:
        print(
            "No config file found. Place soxbox.yaml in ~/.config, /usr/local/etc, "
            "or /etc, or pass --config.",
            file=sys.stderr,
        )
        return 2
    if not os.path.exists(config_path):
        print(f"Config file not found: {config_path}", file=sys.stderr)
        return 2

    config = load_config(config_path) or {}
    region = config.get("region") or DEFAULT_REGION
    aws_profile = config.get("aws_profile")
    local_port = config.get("local_socks_port") or find_free_port()

    # Equivalent of `aws configure` / `aws sso login` credentials: boto3.Session
    # walks the default credential provider chain (env vars, ~/.aws/credentials,
    # ~/.aws/config, SSO cache, IAM role, etc.). If the cached SSO token is
    # expired, get_session reruns `aws sso login` so the user can reauthenticate.
    try:
        session = get_session(region, aws_profile)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        print("Run `aws configure` or `aws configure sso` to set up credentials.", file=sys.stderr)
        return 2
    ec2 = session.client("ec2")

    key_pair = config.get("key_pair")
    identity_file = config.get("identity_file")
    if key_pair and identity_file:
        identity_file = os.path.abspath(os.path.expanduser(identity_file))
        if not os.path.exists(identity_file):
            print(f"Identity file not found: {identity_file}", file=sys.stderr)
            return 2
    elif not key_pair and not identity_file:
        key_pair = DEFAULT_KEY_NAME
        identity_file = ensure_keypair(ec2, key_pair)
    else:
        print("Set both key_pair and identity_file in config, or neither.", file=sys.stderr)
        return 2

    security_group = config.get("security_group") or ensure_security_group(ec2, DEFAULT_SG_NAME)

    ami_id = config.get("ami_id")
    ssh_user = config.get("ssh_user")
    if not ami_id:
        ami_id, default_ssh_user = find_default_ami(ec2)
        ssh_user = ssh_user or default_ssh_user
    ssh_user = ssh_user or "ec2-user"

    instance_type = config.get("instance_type") or find_default_instance_type(ec2)

    config = {
        **config,
        "ami_id": ami_id,
        "instance_type": instance_type,
        "key_pair": key_pair,
        "security_group": security_group,
    }

    instance_id = None
    ssh_proc = None
    profile_dir = None
    try:
        instance_id = launch_instance(ec2, config)
        host = wait_for_running(ec2, instance_id)
        print(f"Instance hostname: {host}")
        wait_for_ssh(host)

        ssh_proc = start_ssh_tunnel(host, identity_file, ssh_user, local_port)
        time.sleep(3)
        if ssh_proc.poll() is not None:
            raise RuntimeError(f"SSH tunnel exited prematurely (code {ssh_proc.returncode})")

        profile_dir = make_firefox_profile(local_port)
        print(f"Launching Firefox (private mode) via SOCKS5 127.0.0.1:{local_port}...")
        firefox = subprocess.Popen([
            "firefox",
            "-profile", profile_dir,
            "--no-remote",
            "--private-window",
        ])
        firefox.wait()
        print("Firefox closed.")
    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        if ssh_proc and ssh_proc.poll() is None:
            print("Closing SSH tunnel...")
            ssh_proc.terminate()
            try:
                ssh_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                ssh_proc.kill()
        if profile_dir and os.path.isdir(profile_dir):
            shutil.rmtree(profile_dir, ignore_errors=True)
        if instance_id:
            terminate_instance(ec2, instance_id)

    return 0


if __name__ == "__main__":
    sys.exit(main())
