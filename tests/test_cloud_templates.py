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


def _aws_user_data(aws, admin_ipv6: str = "") -> str:
    template, variables = aws["Resources"]["Instance"]["Properties"]["UserData"]["Fn::Base64"][
        "Fn::Sub"
    ]
    assert set(variables) == {"AdminIpv6"}  # ",<network>" when given, else ""
    values = {**SAMPLE, "AdminIpv6": f",{admin_ipv6}" if admin_ipv6 else ""}
    return re.sub(r"\$\{(\w+)\}", lambda m: values[m.group(1)], template)


def test_aws_template_installs_the_release_with_the_expected_settings(aws):
    params = aws["Parameters"]
    assert params["Version"]["Default"] == VERSION
    check_cloud_init(_aws_user_data(aws))
    with_ipv6 = yaml.safe_load(_aws_user_data(aws, "2001:db8:1234::/64"))["write_files"][0]
    assert "PORTAL_ALLOWED_IPS=203.0.113.9/32,2001:db8:1234::/64" in with_ipv6["content"]


def test_aws_image_is_resolved_once_at_creation(aws):
    """A stack update must never re-resolve the image and so replace the instance."""
    resources = aws["Resources"]
    instance = resources["Instance"]
    assert instance["Properties"]["ImageId"] == {"Fn::GetAtt": "UbuntuImage.ImageId"}
    assert instance["UpdateReplacePolicy"] == "Retain"
    assert aws["Parameters"]["UbuntuAmiParameter"]["Type"] == "String"  # not an SSM type
    # Its logs go to a log group the stack owns, removed with the stack.
    logs = resources["ImageResolverLogGroup"]
    assert logs["DeletionPolicy"] == "Delete" and logs["Properties"]["RetentionInDays"] == 14
    function = resources["ImageResolverFunction"]["Properties"]
    assert function["LoggingConfig"]["LogGroup"] == {"Ref": "ImageResolverLogGroup"}
    role = resources["ImageResolverRole"]["Properties"]
    assert "ManagedPolicyArns" not in role  # no logs:CreateLogGroup to recreate it
    actions = {
        a
        for statement in role["Policies"][0]["PolicyDocument"]["Statement"]
        for a in (
            statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]]
        )
    }
    assert actions == {"ssm:GetParameter", "logs:CreateLogStream", "logs:PutLogEvents"}
    code = resources["ImageResolverFunction"]["Properties"]["Code"]["ZipFile"]

    import sys
    import types

    calls, responses = [], []

    class FakeSsm:
        def get_parameter(self, Name):  # noqa: N803 (the boto3 signature)
            calls.append(Name)
            if Name == "/missing":
                raise KeyError(Name)
            return {"Parameter": {"Value": "ami-0123456789abcdef0"}}

    fake_boto3 = types.SimpleNamespace(client=lambda service: FakeSsm())
    namespace: dict = {}
    real = sys.modules.get("boto3")
    sys.modules["boto3"] = fake_boto3  # the Lambda runtime provides boto3
    try:
        exec(compile(code, "index.py", "exec"), namespace)  # noqa: S102 (the template's own code)
    finally:
        if real is None:
            del sys.modules["boto3"]
        else:
            sys.modules["boto3"] = real
    namespace["urllib"].request.urlopen = lambda request, timeout: responses.append(
        json.loads(request.data)
    )

    def event(kind, parameter="/aws/service/canonical/x", physical=None):
        body = {
            "RequestType": kind,
            "ResourceProperties": {"Parameter": parameter},
            "StackId": "stack",
            "RequestId": "req",
            "LogicalResourceId": "UbuntuImage",
            "ResponseURL": "https://example.invalid/response",
        }
        if physical:
            body["PhysicalResourceId"] = physical
        return body

    namespace["handler"](event("Create"), None)
    namespace["handler"](event("Update", physical="ami-0123456789abcdef0"), None)
    namespace["handler"](event("Delete", physical="ami-0123456789abcdef0"), None)
    namespace["handler"](event("Create", parameter="/missing"), None)
    assert calls == ["/aws/service/canonical/x", "/missing"]  # read on Create only
    created, updated, deleted, failed = responses
    assert created["Status"] == "SUCCESS" and created["Data"]["ImageId"] == "ami-0123456789abcdef0"
    assert updated["Status"] == "SUCCESS" and updated["Data"]["ImageId"] == "ami-0123456789abcdef0"
    assert updated["PhysicalResourceId"] == "ami-0123456789abcdef0"  # unchanged: no replacement
    assert deleted["Status"] == "SUCCESS"
    assert failed["Status"] == "FAILED" and "KeyError" in failed["Reason"]


def test_aws_template_network_and_access(aws):
    resources = aws["Resources"]
    ingress = resources["SecurityGroup"]["Properties"]["SecurityGroupIngress"]
    public = {
        (r["IpProtocol"], r["FromPort"], r.get("CidrIp") or r.get("CidrIpv6"))
        for r in ingress
        if "FromPort" in r and r["FromPort"] != 22
    }
    assert public == {
        (proto, port, cidr)
        for proto, port in (("tcp", 80), ("tcp", 443), ("udp", 443))
        for cidr in ("0.0.0.0/0", "::/0")
    }
    ssh = [r for r in ingress if isinstance(r, dict) and r.get("FromPort") == 22]
    assert ssh[0]["CidrIp"] == {"Ref": "AdminCidr"}  # SSH only from the admin address
    (optional,) = [r for r in ingress if "Fn::If" in r]
    assert optional["Fn::If"][0] == "HasAdminIpv6"
    assert optional["Fn::If"][1]["CidrIpv6"] == {"Ref": "AdminCidrIpv6"}
    instance = resources["Instance"]["Properties"]
    assert instance["MetadataOptions"]["HttpTokens"] == "required"  # IMDSv2
    assert instance["BlockDeviceMappings"][0]["Ebs"]["Encrypted"] is True
    # The addresses customers' DNS points at belong to the stack, not the instance.
    assert resources["ElasticIp"]["Type"] == "AWS::EC2::EIP"
    assert resources["NetworkInterface"]["Properties"]["Ipv6AddressCount"] == 1
    admin = re.compile(aws["Parameters"]["AdminCidr"]["AllowedPattern"])
    for good in ("203.0.113.9/32", "198.51.100.0/24", "10.0.0.0/8", "255.255.255.255/32"):
        assert admin.match(good), good
    for bad in ("0.0.0.0/0", "203.0.113.9", "999.1.1.1/32", "203.0.113.9/1", "203.0.113.9/7"):
        assert not admin.match(bad), bad
    admin6 = re.compile(aws["Parameters"]["AdminCidrIpv6"]["AllowedPattern"])
    for good in ("", "2001:db8:1234::/64", "2001:db8::1/128", "2001:db8::/16"):
        assert admin6.match(good), good
    for bad in ("::/0", "2001:db8::/8", "203.0.113.9/32", "2001:db8::"):
        assert not admin6.match(bad), bad


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


def test_azure_template_validates_what_reaches_the_nsg_and_the_installer():
    """ARM has no patterns: invalid values must fail through the checked variables."""
    arm = json.loads(AZURE_JSON.read_text())
    variables = arm["variables"]
    for name, parameter, message in (
        # ARM names the failing variable in its error, so the names explain it.
        (
            "adminCidr_must_be_an_IPv4_network_8_to_32",
            "adminCidr",
            "adminCidr must be an IPv4 address with a prefix of /8",
        ),
        (
            "adminCidrIpv6_must_be_empty_or_an_IPv6_network_16_to_128",
            "adminCidrIpv6",
            "adminCidrIpv6 must be empty or an IPv6 network",
        ),
        (
            "version_must_be_a_release_like_0_6_0_or_latest",
            "version",
            "version must be a release such as",
        ),
    ):
        expression = variables[name]
        assert expression.startswith("[if(variables("), name  # only one branch is evaluated
        assert f"parameters('{parameter}')" in expression and "bool('" + message in expression
    # The IPv6 network may only contain hex digits and colons (checked per character).
    chars = json.dumps(variables["copy"]) if "copy" in variables else json.dumps(variables)
    assert "0123456789abcdefABCDEF:" in chars and "adminIpv6CharsValid" in chars
    assert "adminIpv6CharsValid" in variables["adminIpv6Valid"]
    install_env = json.dumps(variables["installEnv"])
    assert "variables('version_must_be_a_release_like_0_6_0_or_latest')" in install_env
    assert "variables('portalAllowed')" in install_env
    assert "parameters('version')" not in install_env
    assert "parameters('adminCidr')" not in install_env


def test_azure_template_network_and_access():
    arm = json.loads(AZURE_JSON.read_text())
    resources = {r["type"]: r for r in arm["resources"]}
    # The rules are one expression (the IPv6 SSH rule only when given); read the
    # fixed ones from the Bicep source, the compiled form is checked below.
    bicep = AZURE_BICEP.read_text()
    rules = {
        m.group(1): m.group(2)
        for m in re.finditer(r"name: '([\w-]+)'\n\s+properties: \{(.*?)\n\s+\}", bicep, re.S)
    }
    assert "destinationPortRanges: [ '80', '443' ]" in rules["http-https"]
    assert "protocol: 'Udp'" in rules["http3"] and "destinationPortRange: '443'" in rules["http3"]
    assert "sourceAddressPrefix: adminCidr_must_be_an_IPv4_network_8_to_32\n" in rules["ssh-admin"]
    assert (
        "sourceAddressPrefix: adminCidrIpv6_must_be_empty_or_an_IPv6_network_16_to_128\n"
        in rules["ssh-admin-ipv6"]
    )
    # SSH comes only from the validated admin networks, never the raw parameter.
    assert "[parameters('adminCidr')]" not in AZURE_JSON.read_text().split('"resources"')[1]
    nsg = json.dumps(resources["Microsoft.Network/networkSecurityGroups"]["properties"])
    assert "variables('adminCidr_must_be_an_IPv4_network_8_to_32')" in nsg
    assert "variables('adminCidrIpv6_must_be_empty_or_an_IPv6_network_16_to_128')" in nsg
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
    assert "custom-domain-ssh-ipv6" not in calls.replace("describe custom-domain-ssh-ipv6", "")
    assert "sudo custom-domain doctor" in result.stdout
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

    # A new admin network on a re-run updates the SSH rule and says how to
    # update the portal allowlist on the VM.
    (tmp_path / "calls.log").unlink()
    moved = run_gcp(tmp_path, ADMIN_CIDR="198.51.100.0/24")
    assert moved.returncode == 0, moved.stderr
    calls = (tmp_path / "calls.log").read_text()
    assert (
        "firewall-rules update custom-domain-ssh --source-ranges 198.51.100.0/24,35.235.240.0/20"
        in calls
    )
    assert "PORTAL_ALLOWED_IPS=198.51.100.0/24" in moved.stdout


def test_gcp_script_admits_an_ipv6_admin_network(tmp_path):
    result = run_gcp(tmp_path, ADMIN_CIDR_IPV6="2001:db8:1234::/64")
    assert result.returncode == 0, result.stderr
    content = yaml.safe_load((tmp_path / "user-data").read_text())["write_files"][0]["content"]
    assert "PORTAL_ALLOWED_IPS=203.0.113.9/32,2001:db8:1234::/64" in content
    calls = (tmp_path / "calls.log").read_text()
    # Firewall rules take one address family each.
    assert "create custom-domain-ssh-ipv6" in calls
    assert "--allow tcp:22 --source-ranges 2001:db8:1234::/64" in calls


@pytest.mark.parametrize(
    "env, message",
    [
        ({"ADMIN_CIDR": "0.0.0.0/0"}, "ADMIN_CIDR"),
        ({"ADMIN_CIDR": "999.1.1.1/32"}, "ADMIN_CIDR"),
        ({"ADMIN_CIDR": "203.0.113.9/1"}, "ADMIN_CIDR"),
        ({"ADMIN_CIDR_IPV6": "::/0"}, "ADMIN_CIDR_IPV6"),
        ({"ADMIN_CIDR_IPV6": "2001:db8::"}, "ADMIN_CIDR_IPV6"),
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
