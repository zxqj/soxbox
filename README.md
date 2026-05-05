# soxbox

Spin up a throwaway EC2 instance, open an SSH SOCKS5 tunnel through it, and
launch Firefox (in private mode) configured to send all traffic through that
tunnel. When Firefox exits, the SSH tunnel is closed and the instance is
terminated.

## Requirements

- Python 3.14+
- An `ssh` client and `firefox` on `PATH`
- AWS credentials available to boto3 (one of: `aws configure`, `aws sso login`,
  `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` env vars, instance role)
- An EC2 key pair whose `.pem` file is on disk with mode `0600`
- A security group (the `default` one is fine) that allows inbound TCP/22 from
  your IP

## Setup

```sh
# 1. Authenticate against AWS. Either of these works:
aws configure          # static IAM keys
aws sso login          # SSO

# 2. Create your config. soxbox looks for a `soxbox.yaml` in
#    ~/.config, /usr/local/etc, and /etc — in that order of precedence.
cp example.soxbox.yaml ~/.config/soxbox.yaml
$EDITOR ~/.config/soxbox.yaml    # set ami_id, key_pair, identity_file, region, etc.

# 3. Make sure the identity file has the right permissions
chmod 600 ./private_key.pem
```

## Install

`soxbox` is packaged as a uv tool, so you can install it once and call it from
anywhere:

```sh
# Install globally (puts ./soxbox on PATH at ~/.local/bin/soxbox)
uv tool install .

# Run it (uses discovered soxbox.yaml, or pass --config)
soxbox
soxbox --config /path/to/other.yaml

# Upgrade after pulling changes
uv tool install --force .

# Uninstall
uv tool uninstall soxbox
```

To run it ephemerally without installing:

```sh
uvx --from . soxbox
```

If/when this is published to PyPI, plain `uvx soxbox` will work too.

For development without `uv tool install`:

```sh
uv sync
uv run soxbox
#   or: uv run python soxbox.py
```

What happens:

1. boto3 launches a single EC2 instance using the parameters in `soxbox.yaml`.
2. soxbox waits for the instance to reach `running` and for TCP/22 to accept
   connections.
3. `ssh -D <local_port> -N` is launched, creating a local SOCKS5 listener on
   `127.0.0.1:<local_port>`.
4. A throwaway Firefox profile is created with `network.proxy.*` prefs pointing
   at that SOCKS5 listener (with `socks_remote_dns` enabled so DNS goes over
   the tunnel too).
5. Firefox is launched against that profile in a private window.
6. When you close Firefox (or hit Ctrl+C), soxbox tears down the SSH tunnel,
   removes the temporary profile, and terminates the EC2 instance.

## Config reference

| Key | Description |
| --- | --- |
| `region` | AWS region. The AMI must exist here. |
| `aws_profile` | Optional named profile from `~/.aws/credentials`. `null` uses the default chain. |
| `security_group` | Security group name (e.g. `default`). |
| `ami_id` | AMI ID to launch. |
| `key_pair` | EC2 key pair name. |
| `identity_file` | Path to the matching `.pem` private key on disk. |
| `instance_type` | e.g. `t2.nano`. |
| `ssh_user` | Login user for the AMI (`ec2-user`, `ubuntu`, `admin`, ...). |
| `local_socks_port` | Local SOCKS5 port. `null` picks a free one. |

## Notes

- `config.yaml`, `soxbox.yaml`, and `*.pem` are gitignored — keep them that way.
- If the script is killed forcefully (`kill -9`), the instance will *not* be
  terminated. Check the EC2 console or run `aws ec2 describe-instances
  --filters Name=tag:Name,Values=soxbox` to find any leftovers.
