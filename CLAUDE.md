# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```sh
uv sync                        # install deps into .venv
uv run python soxbox.py        # run from source (needs ./config.yaml)
uv run python soxbox.py --config path/to.yaml

uv tool install --force .      # install/upgrade as global tool → ~/.local/bin/soxbox
uvx --from . soxbox            # ephemeral run from local checkout
```

There are no tests or linters configured.

## Architecture

Single-file CLI (`soxbox.py`) that orchestrates four resources whose lifetimes
are nested and must unwind in the right order:

```
EC2 instance ─► SSH SOCKS tunnel ─► Firefox temp profile ─► Firefox process
       (boto3)          (subprocess ssh -D)     (tempfile)         (subprocess)
```

The whole flow lives inside one `try/finally` in `main()`. The `finally`
block runs cleanup in reverse: terminate Firefox-owned resources (tunnel +
temp profile), then `terminate_instances`. **Anything added to the happy
path must have a matching teardown in that `finally`** — a forced kill
(`SIGKILL`, OOM, power loss) will leak a running EC2 instance, which costs
money. Instances are tagged `Name=soxbox` so leaks can be found with
`aws ec2 describe-instances --filters Name=tag:Name,Values=soxbox`.

Firefox has no CLI flag for SOCKS configuration. The proxy is wired up by
generating a throwaway profile dir (`tempfile.mkdtemp`) containing a
`user.js` that sets `network.proxy.{type,socks,socks_port,socks_version,socks_remote_dns}`,
then launching `firefox -profile <dir> --no-remote --private-window`.
`socks_remote_dns=true` is load-bearing — without it DNS leaks outside the
tunnel.

`boto3.Session(region_name=…, profile_name=…)` uses the default credential
provider chain (env vars, `~/.aws/credentials`, SSO cache, IAM role). The
"login" credential type that `aws configure` writes by default requires the
`botocore[crt]` extra — it's pinned in `pyproject.toml` for that reason.

## Config (`soxbox.conf`)

YAML-format config file. Discovered (highest precedence first) at
`~/.config/soxbox.conf`, `/usr/local/etc/soxbox.conf`, `/etc/soxbox.conf`;
`--config <path>` overrides discovery. Discovery is "first match wins" —
files are not merged.

The five fields the user originally specified (`security_group`, `ami_id`,
`key_pair`, `identity_file`, `instance_type`) plus pragmatic additions
required to actually work: `region` (boto3 needs one and AMIs are
region-scoped), `ssh_user` (depends on the AMI: `ec2-user` for Amazon Linux,
`ubuntu` for Ubuntu, `admin` for Debian), `aws_profile` (optional named
profile), and `local_socks_port` (null → pick a free port).

`config.yaml`, `soxbox.conf`, and `*.pem` are gitignored.
`soxbox.example.conf` is the checked-in template with dummy values.

## Operational gotchas

- The configured AMI must exist in the configured region, or `RunInstances`
  fails with `InvalidAMIID.NotFound`. Cross-check with
  `aws ec2 describe-images --image-ids <id> --region <r>`.
- The chosen security group must allow inbound TCP/22 from the caller's
  current public IP, or the script times out at the "Waiting for SSH" step
  (240s) and tears the instance back down. The `default` SG usually does
  not — `launch-wizard-1` typically does.
- `pyproject.toml` declares `[build-system]` with hatchling and
  `only-include = ["soxbox.py"]`. The single-module layout means renaming
  `soxbox.py` requires updating both the script entry point
  (`soxbox = "soxbox:main"`) and the `only-include` list.
