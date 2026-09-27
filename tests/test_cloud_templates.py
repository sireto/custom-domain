"""The one-click cloud templates: AWS CloudFormation, Azure Bicep/ARM and the GCP script.

None of them is deployed here. Each is checked for what reaches the server
(the cloud-init that runs deploy/install.sh), for the network it opens, and
for staying pinned to the current release.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
AWS = ROOT / "deploy" / "aws" / "custom-domain.yaml"
AZURE_BICEP = ROOT / "deploy" / "azure" / "main.bicep"
AZURE_JSON = ROOT / "deploy" / "azure" / "azuredeploy.json"
GCP = ROOT / "deploy" / "gcp" / "deploy.sh"
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]

SAMPLE = {
    "EdgeHostname": "edge.example.net",
    "AcmeEmail": "ops@example.net",
    "AdminCidr": "203.0.113.9/32",
    "Version": VERSION,
}
EXPECTED_ENV = {
    "ACME_EMAIL": "ops@example.net",
    "EDGE_HOSTNAME": "edge.example.net",
    "CUSTOM_DOMAIN_VERSION": VERSION,
    "PORTAL_ALLOWED_IPS": "203.0.113.9/32",
    "PUBLIC_API": "true",
    # The cloud's own firewall (security group, NSG, VPC rules) is in front.
    "SKIP_FIREWALL": "1",
}
INSTALLER = "https://raw.githubusercontent.com/sireto/custom-domain/$ref/deploy/install.sh"


def env_lines(text: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


def check_cloud_init(document: str) -> None:
    """The rendered cloud-config installs the release with the expected settings."""
    assert document.startswith("#cloud-config\n")
    config = yaml.safe_load(document)
    (install_env,) = [f for f in config["write_files"] if f["path"].endswith("install.env")]
    assert install_env["permissions"] == "0600"
    content = install_env["content"]
    if install_env.get("encoding") == "b64":
        content = base64.b64decode(content).decode()
    assert env_lines(content) == EXPECTED_ENV
    fetch, run = (step[2] for step in config["runcmd"])
    assert INSTALLER in fetch and '[ "$ref" = latest ] && ref=main' in fetch
    assert run == "bash /root/custom-domain-install.sh >/var/log/custom-domain-install.log 2>&1"


# --- AWS ------------------------------------------------------------------------------


class CfnLoader(yaml.SafeLoader):
    """Reads CloudFormation's short-form tags as {"Fn::X": value}."""


def _tag(loader, suffix, node):
    name = "Ref" if suffix == "Ref" else f"Fn::{suffix}"
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    else:
        value = loader.construct_mapping(node, deep=True)
    return {name: value}


CfnLoader.add_multi_constructor("!", _tag)


@pytest.fixture(scope="module")
def aws():
    return yaml.load(AWS.read_text(), Loader=CfnLoader)  # noqa: S506 (a SafeLoader subclass)


def test_aws_template_installs_the_release_with_the_expected_settings(aws):
    params = aws["Parameters"]
    assert params["Version"]["Default"] == VERSION
    user_data = aws["Resources"]["Instance"]["Properties"]["UserData"]["Fn::Base64"]["Fn::Sub"]
    rendered = re.sub(r"\$\{(\w+)\}", lambda m: SAMPLE[m.group(1)], user_data)
    check_cloud_init(rendered)


def test_aws_template_network_and_access(aws):
    resources = aws["Resources"]
    ingress = resources["SecurityGroup"]["Properties"]["SecurityGroupIngress"]
    public = {
        (r["IpProtocol"], r["FromPort"], r.get("CidrIp") or r.get("CidrIpv6"))
        for r in ingress
        if r["FromPort"] != 22
    }
    assert public == {
        (proto, port, cidr)
        for proto, port in (("tcp", 80), ("tcp", 443), ("udp", 443))
        for cidr in ("0.0.0.0/0", "::/0")
    }
    (ssh,) = [r for r in ingress if r["FromPort"] == 22]
    assert ssh["CidrIp"] == {"Ref": "AdminCidr"}  # SSH only from the admin address
    instance = resources["Instance"]["Properties"]
    assert instance["MetadataOptions"]["HttpTokens"] == "required"  # IMDSv2
    assert instance["BlockDeviceMappings"][0]["Ebs"]["Encrypted"] is True
    # The addresses customers' DNS points at belong to the stack, not the instance.
    assert resources["ElasticIp"]["Type"] == "AWS::EC2::EIP"
    assert resources["NetworkInterface"]["Properties"]["Ipv6AddressCount"] == 1
    admin = re.compile(aws["Parameters"]["AdminCidr"]["AllowedPattern"])
    assert admin.match("203.0.113.9/32") and admin.match("198.51.100.0/24")
    assert not admin.match("0.0.0.0/0") and not admin.match("203.0.113.9")


# --- Azure ----------------------------------------------------------------------------


def _bicep_cloud_init() -> str:
    source = AZURE_BICEP.read_text()
    template = re.search(r"var cloudInit = format\('''(.*?)''', ", source, re.S).group(1)
    env = "\n".join(f"{k}={v}" for k, v in EXPECTED_ENV.items()) + "\n"
    return template.replace("{0}", base64.b64encode(env.encode()).decode())


def test_azure_template_installs_the_release_with_the_expected_settings():
    check_cloud_init(_bicep_cloud_init())
    source = AZURE_BICEP.read_text()
    assert f"param version string = '{VERSION}'" in source
    # The Bicep builds the environment from the same keys.
    keys = re.findall(r"'([A-Z_]+)=", source.split("var installEnv")[1].split("])")[0])
    assert keys == list(EXPECTED_ENV)


def test_azure_json_is_compiled_from_the_bicep():
    """The Deploy to Azure button uses the JSON: it must match the Bicep source."""
    arm = json.loads(AZURE_JSON.read_text())
    assert arm["parameters"]["version"]["defaultValue"] == VERSION
    bicep = AZURE_BICEP.read_text()
    assert set(arm["parameters"]) == set(re.findall(r"^param (\w+) ", bicep, re.M))
    cloud_init = re.search(r"var cloudInit = format\('''(.*?)''', ", bicep, re.S).group(1)
    assert json.dumps(cloud_init)[1:-1] in AZURE_JSON.read_text(), (
        "azuredeploy.json is stale: az bicep build --file deploy/azure/main.bicep "
        "--outfile deploy/azure/azuredeploy.json"
    )


def test_azure_template_network_and_access():
    arm = json.loads(AZURE_JSON.read_text())
    resources = {r["type"]: r for r in arm["resources"]}
    rules = {
        r["name"]: r["properties"]
        for r in resources["Microsoft.Network/networkSecurityGroups"]["properties"]["securityRules"]
    }
    assert rules["http-https"]["destinationPortRanges"] == ["80", "443"]
    assert rules["http3"]["protocol"] == "Udp" and rules["http3"]["destinationPortRange"] == "443"
    assert rules["ssh-admin"]["sourceAddressPrefix"] == "[parameters('adminCidr')]"
    vm = resources["Microsoft.Compute/virtualMachines"]["properties"]
    assert vm["osProfile"]["linuxConfiguration"]["disablePasswordAuthentication"] is True
    ips = [r for r in arm["resources"] if r["type"] == "Microsoft.Network/publicIPAddresses"]
    assert {r["properties"]["publicIPAddressVersion"] for r in ips} == {"IPv4", "IPv6"}
    assert all(r["properties"]["publicIPAllocationMethod"] == "Static" for r in ips)


# --- Google Cloud ---------------------------------------------------------------------

STUB_GCLOUD = r"""#!/usr/bin/env bash
# Records every call; keeps created resources as marker files.
echo "$*" >> "$STATE/calls.log"
args=("$@")
while [ "${args[0]:-}" = --project ] || [ "${args[0]:-}" = --quiet ]; do
    if [ "${args[0]}" = --project ]; then args=("${args[@]:2}"); else args=("${args[@]:1}"); fi
done
case " ${args[*]} " in
  *" config get-value project "*) echo "test-project"; exit 0 ;;
  *" config get-value "*) exit 0 ;;
  *" zones list "*) echo "us-central1-a"; exit 0 ;;
  *" services enable "*) exit 0 ;;
esac
for i in "${!args[@]}"; do
  kind="$(IFS=-; echo "${args[*]:0:$i}")"  # e.g. compute-networks-subnets
  case "${args[$i]}" in
    describe)
      name="${args[$((i + 1))]}"
      [ -e "$STATE/$kind-$name" ] || exit 1
      case " ${args[*]} " in
        *"value(address)"*) [[ "$name" == *ipv6 ]] && echo "2600:1900::7" || echo "34.1.2.3" ;;
      esac
      exit 0 ;;
    create)
      name="${args[$((i + 1))]}"
      touch "$STATE/$kind-$name"
      for a in "${args[@]}"; do
        case "$a" in user-data=*) cp "${a#user-data=}" "$STATE/user-data" ;; esac
      done
      exit 0 ;;
  esac
done
exit 0
"""


def run_gcp(tmp_path: Path, **env: str) -> subprocess.CompletedProcess:
    stubs = tmp_path / "stubs"
    stubs.mkdir(exist_ok=True)
    gcloud = stubs / "gcloud"
    gcloud.write_text(STUB_GCLOUD)
    gcloud.chmod(0o755)
    environment = {
        "PATH": f"{stubs}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "STATE": str(tmp_path),
        "EDGE_HOSTNAME": SAMPLE["EdgeHostname"],
        "ACME_EMAIL": SAMPLE["AcmeEmail"],
        "ADMIN_CIDR": SAMPLE["AdminCidr"],
        **env,
    }
    return subprocess.run(
        ["bash", str(GCP)],
        env=environment,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=60,
    )


def test_gcp_script_creates_the_stack_and_installs_the_release(tmp_path):
    result = run_gcp(tmp_path)
    assert result.returncode == 0, result.stderr + result.stdout
    check_cloud_init((tmp_path / "user-data").read_text())
    calls = (tmp_path / "calls.log").read_text()
    assert "--stack-type IPV4_IPV6" in calls  # dual-stack subnet
    assert "--ip-version IPV6 --endpoint-type VM" in calls  # static IPv6
    assert "--allow tcp:80,tcp:443,udp:443 --source-ranges 0.0.0.0/0" in calls
    assert "--allow tcp:80,tcp:443,udp:443 --source-ranges ::/0" in calls
    assert "--allow tcp:22 --source-ranges 203.0.113.9/32,35.235.240.0/20" in calls
    instance = [line for line in calls.splitlines() if "instances create" in line][0]
    assert "--no-service-account --no-scopes" in instance and "--shielded-secure-boot" in instance
    assert "address=34.1.2.3,external-ipv6-address=2600:1900::7" in instance
    assert "A     34.1.2.3" in result.stdout and "AAAA  2600:1900::7" in result.stdout
    assert "https://edge.example.net/v1" in result.stdout

    # Running it again creates nothing.
    (tmp_path / "calls.log").unlink()
    again = run_gcp(tmp_path)
    assert again.returncode == 0, again.stderr
    assert " create " not in (tmp_path / "calls.log").read_text()
    assert "already exists" in again.stdout


@pytest.mark.parametrize(
    "env, message",
    [
        ({"ADMIN_CIDR": "0.0.0.0/0"}, "ADMIN_CIDR"),
        ({"EDGE_HOSTNAME": "not a name"}, "EDGE_HOSTNAME"),
        ({"ACME_EMAIL": "nobody"}, "ACME_EMAIL"),
        ({"CUSTOM_DOMAIN_VERSION": "main"}, "CUSTOM_DOMAIN_VERSION"),
        ({"ADMIN_CIDR": ""}, "ADMIN_CIDR is required"),
    ],
)
def test_gcp_script_refuses_bad_input_before_creating_anything(tmp_path, env, message):
    result = run_gcp(tmp_path, **env)
    assert result.returncode != 0 and message in result.stderr
    calls = (tmp_path / "calls.log").read_text() if (tmp_path / "calls.log").exists() else ""
    assert " create " not in calls


def test_gcp_script_is_pinned_to_the_release():
    assert f'VERSION="${{CUSTOM_DOMAIN_VERSION:-{VERSION}}}"' in GCP.read_text()
