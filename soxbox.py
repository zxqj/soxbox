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
from pathlib import Path

import boto3
import yaml
from botocore.exceptions import ClientError, NoCredentialsError


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def find_free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


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
    parser.add_argument("--config", default="config.yaml", help="Path to config YAML")
    args = parser.parse_args()

    if not os.path.exists(args.config):
        print(f"Config file not found: {args.config}", file=sys.stderr)
        print("Copy config.example.yaml to config.yaml and edit it.", file=sys.stderr)
        return 2

    config = load_config(args.config)
    region = config.get("region")
    aws_profile = config.get("aws_profile")
    ssh_user = config.get("ssh_user", "ec2-user")
    local_port = config.get("local_socks_port") or find_free_port()
    identity_file = os.path.abspath(os.path.expanduser(config["identity_file"]))

    if not os.path.exists(identity_file):
        print(f"Identity file not found: {identity_file}", file=sys.stderr)
        return 2

    # Equivalent of `aws configure` / `aws sso login` credentials: boto3.Session
    # walks the default credential provider chain (env vars, ~/.aws/credentials,
    # ~/.aws/config, SSO cache, IAM role, etc.).
    try:
        session = boto3.Session(region_name=region, profile_name=aws_profile)
        ec2 = session.client("ec2")
    except NoCredentialsError:
        print("No AWS credentials found. Run `aws configure` or `aws sso login` first.", file=sys.stderr)
        return 2

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
