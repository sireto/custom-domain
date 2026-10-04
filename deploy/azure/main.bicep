// Custom Domain on one Azure VM: PostgreSQL, the certificate store, the API,
// the worker and the Caddy edge, installed by deploy/install.sh from the
// release named by `version`. Static public IPv4 and IPv6 addresses are what
// customers' CNAMEs resolve to. Compile with `az bicep build --file main.bicep
// --outfile azuredeploy.json` (the "Deploy to Azure" button uses the JSON).

@description('The edge\'s own DNS name, for example edge.example.net. After deployment, point it at the addresses in the outputs (A and AAAA records).')
param edgeHostname string

@description('Where Let\'s Encrypt sends certificate notices. A real address: Let\'s Encrypt refuses reserved domains such as example.com.')
param acmeEmail string

@description('The IPv4 address or network you administer from, for example 203.0.113.9/32 (a prefix of /8 to /32). It may open the portal at https://<edge hostname>/portal and connect with SSH.')
@minLength(9)
@maxLength(18)
param adminCidr string

@description('Optional. Your IPv6 network, for example 2001:db8:1234::/64 (a prefix of /16 to /128). Add it if your connection has IPv6: your browser then prefers the edge\'s IPv6 address, and the portal only admits listed addresses.')
@maxLength(43)
param adminCidrIpv6 string = ''

@description('Your SSH public key (ssh-ed25519 or ssh-rsa ...), for the admin user.')
param adminSshPublicKey string

@description('The admin user on the VM.')
param adminUsername string = 'azureuser'

@description('Standard_B2s (2 vCPU, 4 GB) is enough to start; the edge is I/O bound.')
@allowed([
  'Standard_B2s'
  'Standard_B2ms'
  'Standard_B4ms'
  'Standard_D2s_v5'
  'Standard_D4s_v5'
])
param vmSize string = 'Standard_B2s'

@description('The Custom Domain release to install, such as 0.6.0, or latest (image, installer and SDK share one version number).')
@maxLength(20)
param version string = '0.7.0'

@description('Name prefix for the resources.')
param name string = 'custom-domain'

param location string = resourceGroup().location

// --- input validation ------------------------------------------------------------
// ARM has no regular expressions, so the admin networks and the version are
// checked with expressions. They are written into the NSG and into the file
// the installer sources as root, so an invalid value stops the deployment:
// bool() of a message fails, and if() evaluates only the branch it takes.
// ARM reports the failing variable by name, so the names say what is wrong
// ("The template variable 'adminCidr_must_be_an_IPv4_network_8_to_32' is not
// valid").

var adminParts = split(adminCidr, '/')
var adminOctets = split(adminParts[0], '.')
var adminShapeValid = length(adminParts) == 2 && length(adminOctets) == 4
// Placeholders keep a malformed value on the path to the readable message.
var adminPrefix = adminShapeValid ? adminParts[1] : '0'
var adminOctetValues = adminShapeValid ? adminOctets : [ '0', '0', '0', '0' ]
var adminOctetsValid = [for octet in adminOctetValues: int(octet) >= 0 && int(octet) <= 255 && string(int(octet)) == octet]
var adminCidrValid = adminShapeValid && !contains(adminOctetsValid, false) && int(adminPrefix) >= 8 && int(adminPrefix) <= 32 && string(int(adminPrefix)) == adminPrefix
var adminCidr_must_be_an_IPv4_network_8_to_32 = adminCidrValid ? adminCidr : string(bool('adminCidr must be an IPv4 address with a prefix of /8 to /32, for example 203.0.113.9/32'))

var adminIpv6Parts = split(adminCidrIpv6, '/')
var adminIpv6Prefix = length(adminIpv6Parts) == 2 ? adminIpv6Parts[1] : '0'
var adminIpv6Address = length(adminIpv6Parts) == 2 ? adminIpv6Parts[0] : ''
// Hex digits and colons only: the value is written into a file sourced as root.
var adminIpv6CharsValid = [for i in range(0, length(adminIpv6Address)): contains('0123456789abcdefABCDEF:', substring(adminIpv6Address, i, 1))]
var adminIpv6Valid = empty(adminCidrIpv6) || (length(adminIpv6Parts) == 2 && contains(adminIpv6Address, ':') && !contains(adminIpv6CharsValid, false) && int(adminIpv6Prefix) >= 16 && int(adminIpv6Prefix) <= 128 && string(int(adminIpv6Prefix)) == adminIpv6Prefix)
var adminCidrIpv6_must_be_empty_or_an_IPv6_network_16_to_128 = adminIpv6Valid ? adminCidrIpv6 : string(bool('adminCidrIpv6 must be empty or an IPv6 network with a prefix of /16 to /128, for example 2001:db8:1234::/64'))

var acmeEmailDomain = toLower(last(split(acmeEmail, '@')))
var acmeEmailTld = last(split(acmeEmailDomain, '.'))
// Let's Encrypt refuses reserved contact domains (invalidContact), and no
// certificate could ever be issued.
var acmeEmailValid = contains(acmeEmail, '@') && contains(acmeEmailDomain, '.') && !contains([ 'example.com', 'example.net', 'example.org' ], acmeEmailDomain) && !endsWith(acmeEmailDomain, '.example.com') && !endsWith(acmeEmailDomain, '.example.net') && !endsWith(acmeEmailDomain, '.example.org') && !contains([ 'example', 'test', 'invalid', 'localhost', 'local' ], acmeEmailTld)
var acmeEmail_must_be_a_real_address_not_example_com = acmeEmailValid ? acmeEmail : string(bool('acmeEmail must be a real address; Let\'s Encrypt refuses reserved domains such as example.com'))

var versionParts = split(version, '.')
var versionNumbers = length(versionParts) == 3 ? versionParts : [ '-1' ]
var versionNumbersValid = [for part in versionNumbers: int(part) >= 0 && string(int(part)) == part]
var versionValid = version == 'latest' || (length(versionParts) == 3 && !contains(versionNumbersValid, false))
var version_must_be_a_release_like_0_6_0_or_latest = versionValid ? version : string(bool('version must be a release such as 0.6.0, or latest'))

var portalAllowed = empty(adminCidrIpv6_must_be_empty_or_an_IPv6_network_16_to_128) ? adminCidr_must_be_an_IPv4_network_8_to_32 : '${adminCidr_must_be_an_IPv4_network_8_to_32},${adminCidrIpv6_must_be_empty_or_an_IPv6_network_16_to_128}'

var installEnv = join([
  'ACME_EMAIL=${acmeEmail_must_be_a_real_address_not_example_com}'
  'EDGE_HOSTNAME=${edgeHostname}'
  'CUSTOM_DOMAIN_VERSION=${version_must_be_a_release_like_0_6_0_or_latest}'
  'PORTAL_ALLOWED_IPS=${portalAllowed}'
  'PUBLIC_API=true'
  'SKIP_FIREWALL=1'
], '\n')

// The same steps as deploy/cloud-init.yaml.
var cloudInit = format('''#cloud-config
package_update: true
packages: [ca-certificates, curl]
write_files:
  - path: /etc/custom-domain-install.env
    permissions: "0600"
    encoding: b64
    content: {0}
runcmd:
  - [sh, -c, ". /etc/custom-domain-install.env; ref=\"$CUSTOM_DOMAIN_VERSION\"; [ \"$ref\" = latest ] && ref=main; curl -fsSL \"https://raw.githubusercontent.com/sireto/custom-domain/$ref/deploy/install.sh\" -o /root/custom-domain-install.sh"]
  - [sh, -c, "bash /root/custom-domain-install.sh >/var/log/custom-domain-install.log 2>&1"]
''', base64('${installEnv}\n'))

resource ipv4 'Microsoft.Network/publicIPAddresses@2023-11-01' = {
  name: '${name}-ipv4'
  location: location
  sku: { name: 'Standard' }
  properties: {
    publicIPAllocationMethod: 'Static'
    publicIPAddressVersion: 'IPv4'
  }
}

resource ipv6 'Microsoft.Network/publicIPAddresses@2023-11-01' = {
  name: '${name}-ipv6'
  location: location
  sku: { name: 'Standard' }
  properties: {
    publicIPAllocationMethod: 'Static'
    publicIPAddressVersion: 'IPv6'
  }
}

resource nsg 'Microsoft.Network/networkSecurityGroups@2023-11-01' = {
  name: '${name}-nsg'
  location: location
  properties: {
    securityRules: concat([
      {
        name: 'http-https'
        properties: {
          priority: 100
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRanges: [ '80', '443' ]
        }
      }
      {
        name: 'http3'
        properties: {
          priority: 110
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Udp'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '443'
        }
      }
      {
        name: 'ssh-admin'
        properties: {
          priority: 120
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: adminCidr_must_be_an_IPv4_network_8_to_32
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '22'
        }
      }
    ], empty(adminCidrIpv6_must_be_empty_or_an_IPv6_network_16_to_128) ? [] : [
      {
        name: 'ssh-admin-ipv6'
        properties: {
          priority: 130
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: adminCidrIpv6_must_be_empty_or_an_IPv6_network_16_to_128
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '22'
        }
      }
    ])
  }
}

resource vnet 'Microsoft.Network/virtualNetworks@2023-11-01' = {
  name: '${name}-vnet'
  location: location
  properties: {
    addressSpace: { addressPrefixes: [ '10.80.0.0/16', 'fd00:80::/48' ] }
    subnets: [
      {
        name: 'edge'
        properties: {
          addressPrefixes: [ '10.80.1.0/24', 'fd00:80:0:1::/64' ]
          networkSecurityGroup: { id: nsg.id }
        }
      }
    ]
  }
}

resource nic 'Microsoft.Network/networkInterfaces@2023-11-01' = {
  name: '${name}-nic'
  location: location
  properties: {
    ipConfigurations: [
      {
        name: 'ipv4'
        properties: {
          primary: true
          privateIPAddressVersion: 'IPv4'
          privateIPAllocationMethod: 'Dynamic'
          subnet: { id: vnet.properties.subnets[0].id }
          publicIPAddress: { id: ipv4.id }
        }
      }
      {
        name: 'ipv6'
        properties: {
          privateIPAddressVersion: 'IPv6'
          privateIPAllocationMethod: 'Dynamic'
          subnet: { id: vnet.properties.subnets[0].id }
          publicIPAddress: { id: ipv6.id }
        }
      }
    ]
  }
}

resource vm 'Microsoft.Compute/virtualMachines@2024-03-01' = {
  name: '${name}-vm'
  location: location
  properties: {
    hardwareProfile: { vmSize: vmSize }
    osProfile: {
      computerName: name
      adminUsername: adminUsername
      customData: base64(cloudInit)
      linuxConfiguration: {
        disablePasswordAuthentication: true
        ssh: {
          publicKeys: [
            {
              path: '/home/${adminUsername}/.ssh/authorized_keys'
              keyData: adminSshPublicKey
            }
          ]
        }
      }
    }
    storageProfile: {
      imageReference: {
        publisher: 'Canonical'
        offer: 'ubuntu-24_04-lts'
        sku: 'server'
        version: 'latest'
      }
      osDisk: {
        createOption: 'FromImage'
        diskSizeGB: 30
        managedDisk: { storageAccountType: 'StandardSSD_LRS' }
        deleteOption: 'Delete'
      }
    }
    networkProfile: {
      networkInterfaces: [ { id: nic.id } ]
    }
    securityProfile: {
      securityType: 'TrustedLaunch'
      uefiSettings: { secureBootEnabled: true, vTpmEnabled: true }
    }
  }
}

@description('Create an A record for the edge hostname with this address.')
output publicIpv4 string = ipv4.properties.ipAddress

@description('Create an AAAA record for the edge hostname with this address.')
output publicIpv6 string = ipv6.properties.ipAddress

@description('The records to create in your DNS.')
output dnsRecords string = '${edgeHostname} A ${ipv4.properties.ipAddress}  and  ${edgeHostname} AAAA ${ipv6.properties.ipAddress}'

@description('The operator portal, from the admin address, once DNS resolves (allow ten minutes for the install).')
output portalUrl string = 'https://${edgeHostname}/portal'

@description('Where applications call the API with their credential.')
output apiUrl string = 'https://${edgeHostname}/v1'

@description('SSH in, then read the generated portal password with: sudo grep PORTAL_PASSWORD /opt/custom-domain/deploy/.env')
output ssh string = 'ssh ${adminUsername}@${ipv4.properties.ipAddress}'
