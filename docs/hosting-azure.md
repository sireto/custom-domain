# Hosting on Azure

One virtual machine runs the whole service: PostgreSQL, the certificate
store, the API, the worker and the edge. The template
[deploy/azure/azuredeploy.json](../deploy/azure/azuredeploy.json) (compiled
from [main.bicep](../deploy/azure/main.bicep)) creates everything in one
resource group and installs the release you choose. Budget about fifteen
minutes.

## 1. Deploy

[![Deploy to Azure](https://aka.ms/deploytoazurebutton)](https://portal.azure.com/#create/Microsoft.Template/uri/https%3A%2F%2Fraw.githubusercontent.com%2Fsireto%2Fcustom-domain%2Fmain%2Fdeploy%2Fazure%2Fazuredeploy.json)

Choose a subscription, a new resource group and a region, then fill in:

- **Edge hostname**: the name customers will CNAME to, for example
  `edge.example.net`. You create its DNS records in step 2.
- **Acme email**: where Let's Encrypt sends certificate notices.
- **Admin cidr**: your public IPv4 address as a `/32` (search "what is my
  IP"). It may open the portal and connect with SSH.
- **Admin ssh public key**: your SSH public key (`ssh-ed25519 ...`); password
  login is disabled.
- **Vm size**: `Standard_B2s` is enough to start.

From a shell instead:

```
az group create --name custom-domain --location westeurope
az deployment group create --resource-group custom-domain \
  --template-file deploy/azure/main.bicep \
  --parameters edgeHostname=edge.example.net acmeEmail=ops@example.net \
    adminCidr=203.0.113.9/32 adminSshPublicKey="$(cat ~/.ssh/id_ed25519.pub)"
```

What it creates: static public IPv4 and IPv6 addresses (Standard SKU), a
network security group (TCP 80 and 443 and UDP 443 from anywhere, SSH from
your address only), a dual-stack virtual network, and an Ubuntu 24.04 VM
with Trusted Launch. The deployment finishes in a few minutes; the
installation then runs on the VM for about ten more.

## 2. Point the name at it

The deployment's **Outputs** list the records: an `A` record with
`publicIpv4` and an `AAAA` record with `publicIpv6`, both for the edge
hostname.

## 3. Open the portal

Once the name resolves, open the `portalUrl` output
(`https://edge.example.net/portal`) from your address. Read the generated
password over SSH (the `ssh` output):

```
ssh azureuser@<publicIpv4> sudo grep PORTAL_PASSWORD /opt/custom-domain/deploy/.env
```

`sudo tail -f /var/log/custom-domain-install.log` shows the installation if
the portal is not there yet, and `sudo custom-domain doctor` checks
everything. The portal walks you through the first application; applications
call the API at the `apiUrl` output (`https://edge.example.net/v1`).

## Operating

- **Upgrade**: `sudo custom-domain upgrade <version>` on the VM
  ([deployment.md](deployment.md#upgrading)).
- **Backups**: `docs/deployment.md` (database dump and certificate store);
  Azure Blob Storage is a good target.
- **Deleting the resource group** deletes the VM and the addresses. Back up
  first; customers' CNAMEs then point nowhere.
