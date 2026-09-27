# Hosting on AWS

One EC2 instance runs the whole service: PostgreSQL, the certificate store,
the API, the worker and the edge. The CloudFormation template
[deploy/aws/custom-domain.yaml](../deploy/aws/custom-domain.yaml) creates
everything in one stack and installs the release you choose. Budget about
fifteen minutes.

## 1. Create the stack

**One click:** open the Launch Stack link published for this project:

```
https://console.aws.amazon.com/cloudformation/home#/stacks/quickcreate?stackName=custom-domain&templateURL=https://<AWS_TEMPLATES_BUCKET>.s3.amazonaws.com/custom-domain/latest/custom-domain.yaml
```

The link works once the maintainers have set up template publishing (see
[Cloud template publishing setup](deployment.md#cloud-template-publishing-setup));
CloudFormation only opens templates stored in S3.

**Without the link** (three clicks more): download
[custom-domain.yaml](https://raw.githubusercontent.com/sireto/custom-domain/main/deploy/aws/custom-domain.yaml),
then in the CloudFormation console choose **Create stack → With new
resources → Upload a template file**. Or from a shell:

```
aws cloudformation deploy --stack-name custom-domain --capabilities CAPABILITY_IAM \
  --template-file deploy/aws/custom-domain.yaml \
  --parameter-overrides EdgeHostname=edge.example.net AcmeEmail=ops@example.net AdminCidr=203.0.113.9/32
```

Fill in the parameters:

- **Edge hostname**: the name customers will CNAME to, for example
  `edge.example.net`. You create its DNS records in step 2.
- **Contact email**: where Let's Encrypt sends certificate notices.
- **Your IPv4 address**: your public IPv4 address as a `/32` (search "what
  is my IP"), or a network of `/8` to `/32`. It may open the portal and
  connect with SSH. The whole internet is refused.
- **Your IPv6 network**: optional, but add it if your connection has IPv6
  (for example `2001:db8:1234::/64`). Your browser prefers the edge's IPv6
  address once the `AAAA` record exists, and the portal admits only listed
  addresses.
- **Instance type**: `t3.small` is enough to start.
- **Key pair**: optional. Without one, use Session Manager (below).

Acknowledge that the stack creates an IAM role (for Session Manager) and
create it. The stack is ready in about three minutes; the installation then
runs on the instance for about ten more.

What it creates: a VPC with a dual-stack public subnet, a security group
(TCP 80 and 443 and UDP 443 from anywhere, SSH from your addresses only), a
network interface holding an Elastic IP and an IPv6 address, and an Ubuntu
24.04 instance with an encrypted 30 GB volume and IMDSv2 required. The
Ubuntu image is looked up once, when the stack is created (a small Lambda
function reads Canonical's public parameter), so a later stack update never
replaces the instance because a newer image was published.

## 2. Point the name at it

The stack's **Outputs** tab lists the records: an `A` record with
`PublicIpv4` and an `AAAA` record with `PublicIpv6`, both for the edge
hostname.

## 3. Open the portal

Once the name resolves, open the `PortalUrl` output
(`https://edge.example.net/portal`) from your address. The password is
generated on the instance: in the EC2 console choose the instance, **Connect
→ Session Manager**, and run

```
sudo grep PORTAL_PASSWORD /opt/custom-domain/deploy/.env
```

`sudo tail -f /var/log/custom-domain-install.log` shows the installation if
the portal is not there yet, and `sudo custom-domain doctor` checks
everything. The portal walks you through the first application; applications
call the API at the `ApiUrl` output (`https://edge.example.net/v1`).

## Operating

- **Upgrade**: `sudo custom-domain upgrade <version>` on the instance
  ([deployment.md](deployment.md#upgrading)). Updating the stack with a new
  `Version` does not upgrade a running instance.
- **Stack updates** change the instance in place (for example a new instance
  type, with a restart); the database, the certificates and `.env` stay on
  its volume. The instance is never meant to be replaced: its data lives on
  the root volume, and the network interface holding the addresses stays
  attached to it. Should an update ever require a replacement, the old
  instance is kept (`UpdateReplacePolicy: Retain`) rather than deleted.
- **Backups**: `docs/deployment.md` (database dump and certificate store). S3
  is a good target; add a bucket and a policy to the instance role for it.
- **Deleting the stack** deletes the instance, its volume and the addresses.
  Customers' CNAMEs then point nowhere; back up first.
