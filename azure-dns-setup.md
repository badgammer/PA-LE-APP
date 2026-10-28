# Azure DNS Setup Guide

This guide covers creating and managing a public DNS zone in Microsoft Azure DNS using both the Azure portal and Azure CLI, delegating a domain to Azure, adding records, validating resolution, and preparing Azure DNS for DNS-01 automation such as Let's Encrypt.

## Prerequisites

- An Azure subscription.
- A resource group for the DNS zone.
- A registered domain if you want the zone reachable from the public Internet.
- Azure CLI installed and authenticated for CLI examples.
- Permission to manage DNS zones and record sets in the target subscription/resource group.

Set example variables:

```bash
SUBSCRIPTION_ID="00000000-0000-0000-0000-000000000000"
RESOURCE_GROUP="rg-dns-prod"
ZONE_NAME="example.com"
LOCATION="global"

az login
az account set --subscription "$SUBSCRIPTION_ID"
```

> Azure DNS is a global service. The resource group itself still has an Azure region, but public DNS zones are global resources.

## Azure portal (GUI) procedure

Use this procedure if you prefer to configure Azure DNS through the Azure portal instead of the command line.

### 1. Create or select a resource group

1. Sign in to the Azure portal at `https://portal.azure.com`.
2. In the search bar, enter **Resource groups** and open the service.
3. To use an existing resource group, select it and continue to the next section.
4. To create one, select **Create**.
5. Choose the target **Subscription**.
6. Enter a **Resource group** name, such as `rg-dns-prod`.
7. Choose a region for the resource group, such as **Central US**.
8. Select **Review + create**, then **Create**.

> The resource group has a region, but a public Azure DNS zone is a global resource.

### 2. Create a public DNS zone

1. In the Azure portal search bar, enter **DNS zones**.
2. Open **DNS zones**, then select **Create**.
3. On the **Basics** tab, configure:
   - **Subscription:** the subscription that will own the zone.
   - **Resource group:** for example, `rg-dns-prod`.
   - **Name:** the registered domain name, for example `example.com`.
4. If the zone is a child zone, configure the parent-zone options as required. For a normal registered root domain such as `example.com`, use the standard zone creation flow.
5. Select **Review + create**.
6. Confirm validation succeeds, then select **Create**.
7. Select **Go to resource** after deployment completes.

Azure automatically creates the zone's authoritative SOA and NS record sets.

### 3. Record the Azure-assigned name servers

1. Open the DNS zone.
2. On **Overview**, locate **Name servers**.
3. Copy all four Azure-assigned authoritative name servers.

They will resemble the following, but the actual values assigned to your zone will differ:

```text
ns1-xx.azure-dns.com
ns2-xx.azure-dns.net
ns3-xx.azure-dns.org
ns4-xx.azure-dns.info
```

Use the exact four values displayed in the portal. Do not substitute the example values above.

### 4. Delegate the registered domain to Azure DNS

This step is completed at the domain registrar, not in the Azure portal.

1. Sign in to the registrar that manages `example.com`.
2. Open the domain's **Nameservers**, **Name servers**, or **DNS delegation** settings.
3. Replace the existing authoritative name servers with all four name servers shown on the Azure DNS zone's **Overview** page.
4. Save the change.
5. Allow for DNS caching and registrar processing before expecting every recursive resolver to return the new delegation.

Before changing delegation for a production domain, recreate and verify all required DNS records in Azure DNS to avoid an outage.

### 5. Create records in the Azure portal

1. Open **DNS zones** and select `example.com`.
2. On the DNS zone page, select **+ Record set**.
3. Enter the record name, type, TTL, and value.
4. Select **Add** or **OK**, depending on the current portal experience.

#### Example A record

Create `app.example.com`:

- **Name:** `app`
- **Type:** `A`
- **TTL:** `300`
- **TTL unit:** `Seconds`
- **IP address:** `203.0.113.10`

#### Example CNAME record

Create `www.example.com` as an alias:

- **Name:** `www`
- **Type:** `CNAME`
- **TTL:** `300`
- **Alias:** `app.example.com`

A CNAME cannot coexist with most other record types at the same name. Do not create a CNAME at a name that already has an A, AAAA, TXT, MX, or other conflicting record set.

#### Example TXT record

Create a TXT record for testing or external validation:

- **Name:** `validation`
- **Type:** `TXT`
- **TTL:** `300`
- **Value:** `example-validation-value`

For an ACME DNS-01 challenge, the record name is normally `_acme-challenge`, but the application should create and remove the challenge value automatically.

#### Example MX record

Create an apex MX record:

- **Name:** `@` or leave the name blank if the portal represents the zone apex that way.
- **Type:** `MX`
- **TTL:** `3600`
- **Preference:** `10`
- **Mail exchange:** `mail.example.com`

Ensure the mail-exchange hostname resolves to the intended mail service. An MX record must point to a hostname, not directly to an IP address.

### 6. Grant the certificate application access through IAM

For the Palo Alto/Let's Encrypt application, assign access to the workload's managed identity or Microsoft Entra service principal at the individual DNS-zone scope.

1. Open **DNS zones** and select `example.com`.
2. In the zone menu, select **Access control (IAM)**.
3. Select **Add** > **Add role assignment**.
4. On the **Role** tab, search for and select **DNS Zone Contributor**.
5. Select **Next**.
6. For **Assign access to**, choose the identity category used by the application, typically **Managed identity** or **User, group, or service principal**.
7. Select **Select members**.
8. Find and select the application's identity.
9. Select **Review + assign**, then select **Review + assign** again if prompted.
10. Return to **Access control (IAM)** > **Role assignments** and confirm the identity is listed with scope limited to this DNS zone.

`DNS Zone Contributor` is a practical built-in role for DNS automation. For stricter production least privilege, evaluate a custom role limited to the record-set actions the integration needs.

### 7. Validate the GUI configuration

In the Azure portal:

1. Reopen the DNS zone and confirm the expected record sets appear.
2. Open each important record set and verify its TTL and values.
3. On **Overview**, confirm the assigned name servers match the registrar configuration.
4. In **Access control (IAM)**, confirm the automation identity has the expected role at zone scope.

From a workstation, verify public DNS separately because a record appearing in the portal does not prove that public delegation is correct:

```bash
dig example.com NS +short
dig app.example.com A +short
dig _acme-challenge.example.com TXT
```

### 8. Portal troubleshooting

- **Record exists in Azure but is not public:** Compare the registrar's nameservers with the four servers on the zone's **Overview** page.
- **Old value is returned:** Check the record-set TTL and allow cached responses to expire.
- **Application receives authorization errors:** Review **Access control (IAM)** > **Role assignments**, the selected identity, and the assignment scope.
- **Let's Encrypt cannot find the TXT value:** Verify `_acme-challenge` in **Record sets**, then test the authoritative Azure name server directly with `dig`.
- **Wrong Azure zone is updated:** Confirm the application's subscription ID, resource group, zone name, tenant, and identity configuration.

## Azure CLI procedure

The following sections provide the equivalent command-line workflow.

## 1. Create the resource group

Skip this step if you already have one.

```bash
az group create \
  --name "$RESOURCE_GROUP" \
  --location "centralus"
```

## 2. Create a public Azure DNS zone

```bash
az network dns zone create \
  --resource-group "$RESOURCE_GROUP" \
  --name "$ZONE_NAME"
```

Display the Azure-assigned authoritative name servers:

```bash
az network dns zone show \
  --resource-group "$RESOURCE_GROUP" \
  --name "$ZONE_NAME" \
  --query nameServers \
  --output table
```

Azure will return four authoritative name servers. These are the servers to configure at your domain registrar.

## 3. Delegate the domain to Azure DNS

At the registrar for `example.com`:

1. Open the domain's **Nameservers** or **DNS delegation** settings.
2. Replace the registrar's existing authoritative name servers with all four name servers returned by Azure DNS.
3. Save the change.

Do not create arbitrary NS values. Use the exact Azure-assigned name servers shown on the DNS zone.

### Verify delegation

After the registrar change has propagated:

```bash
dig NS example.com +short
```

You can also query one of the Azure authoritative servers directly:

```bash
dig @ns1-XX.azure-dns.com example.com SOA
```

Replace the server with an actual name server assigned to your zone.

> Registrar delegation changes can be cached according to existing DNS TTLs. During migration, verify records in Azure before switching the delegation to avoid an outage.

## 4. Create common DNS records

Azure creates the apex SOA and NS records for the zone. Typical application records can then be added.

### A record

Create `app.example.com` pointing to `203.0.113.10`:

```bash
az network dns record-set a add-record \
  --resource-group "$RESOURCE_GROUP" \
  --zone-name "$ZONE_NAME" \
  --record-set-name "app" \
  --ipv4-address "203.0.113.10"
```

### CNAME record

Create `www.example.com` as an alias for `app.example.com`:

```bash
az network dns record-set cname set-record \
  --resource-group "$RESOURCE_GROUP" \
  --zone-name "$ZONE_NAME" \
  --record-set-name "www" \
  --cname "app.example.com"
```

### TXT record

```bash
az network dns record-set txt add-record \
  --resource-group "$RESOURCE_GROUP" \
  --zone-name "$ZONE_NAME" \
  --record-set-name "validation" \
  --value "example-validation-value"
```

### MX record

```bash
az network dns record-set mx add-record \
  --resource-group "$RESOURCE_GROUP" \
  --zone-name "$ZONE_NAME" \
  --record-set-name "@" \
  --exchange "mail.example.com" \
  --preference 10
```

## 5. Validate DNS resolution

Check records using `dig`:

```bash
dig app.example.com A +short
dig www.example.com CNAME +short
dig example.com NS +short
```

For troubleshooting, inspect the full resolution path:

```bash
dig +trace app.example.com
```

You should verify both:

- The parent zone delegates the domain to the Azure-assigned name servers.
- The required record exists in the Azure DNS zone and returns the expected value.

## 6. Azure DNS permissions for automation

For services that need to modify DNS records automatically, such as an ACME client performing a Let's Encrypt DNS-01 challenge, use a Microsoft Entra workload identity or managed identity rather than a user's credentials.

A straightforward built-in role is **DNS Zone Contributor**, scoped to the individual DNS zone rather than the whole subscription or resource group when possible.

Get the DNS zone resource ID:

```bash
DNS_ZONE_ID=$(az network dns zone show \
  --resource-group "$RESOURCE_GROUP" \
  --name "$ZONE_NAME" \
  --query id \
  --output tsv)
```

For a managed identity or service principal, obtain its object/principal ID, then assign the role at zone scope:

```bash
PRINCIPAL_ID="00000000-0000-0000-0000-000000000000"

az role assignment create \
  --assignee-object-id "$PRINCIPAL_ID" \
  --assignee-principal-type ServicePrincipal \
  --role "DNS Zone Contributor" \
  --scope "$DNS_ZONE_ID"
```

For production environments, consider a custom Azure RBAC role if the application only needs record-set operations and **DNS Zone Contributor** grants more access than required.

## 7. Let's Encrypt DNS-01 considerations

If this Azure DNS zone is being used by a certificate automation application:

1. The ACME client requests a DNS-01 challenge from Let's Encrypt.
2. The client creates a TXT record below `_acme-challenge.example.com` in Azure DNS.
3. Let's Encrypt resolves the TXT record from public DNS.
4. The client completes the challenge and removes the TXT record.

### Operational recommendations

- Scope the automation identity to the required Azure DNS zone.
- Avoid storing interactive Azure user credentials in an application.
- Prefer managed identity when the workload runs on an Azure service that supports it.
- For workloads outside Azure, use an appropriate Microsoft Entra workload identity/service principal and secure its credentials or use workload identity federation where supported.
- Log DNS modifications and role assignments with Azure Activity Log.
- Keep DNS record TTLs appropriate for the service. Lower TTLs may help during migrations but also increase query volume.

## 8. Troubleshooting checklist

### Domain does not resolve from Azure DNS

```bash
dig example.com NS +trace
```

Confirm the registrar/parent zone delegates to the four Azure name servers assigned to the zone.

### Azure has the record, but clients receive the old value

Check the record's TTL and recursive-resolver caching:

```bash
dig app.example.com A
```

### Let's Encrypt cannot find the TXT challenge

Check the authoritative response directly:

```bash
dig _acme-challenge.example.com TXT
```

Then query an Azure authoritative name server assigned to the zone:

```bash
dig @ns1-XX.azure-dns.com _acme-challenge.example.com TXT
```

If the authoritative server has the TXT record but a public recursive lookup does not, investigate delegation and caching. If the authoritative server does not have it, verify the automation identity's Azure RBAC scope and the zone/record name used by the application.

### Permission errors

Review role assignments on the zone:

```bash
az role assignment list \
  --scope "$DNS_ZONE_ID" \
  --all \
  --output table
```

Remember that Azure RBAC changes can require a short period before they are observed by all services.

## 9. Useful Azure CLI commands

List zones:

```bash
az network dns zone list --output table
```

List all record sets in a zone:

```bash
az network dns record-set list \
  --resource-group "$RESOURCE_GROUP" \
  --zone-name "$ZONE_NAME" \
  --output table
```

Show a TXT record set:

```bash
az network dns record-set txt show \
  --resource-group "$RESOURCE_GROUP" \
  --zone-name "$ZONE_NAME" \
  --name "_acme-challenge"
```

## Security baseline

- Apply least-privilege Azure RBAC at DNS-zone scope.
- Use separate identities for production and non-production automation.
- Prefer managed identity or federated identity over long-lived client secrets where feasible.
- Protect the Azure subscription and privileged identities with Microsoft Entra controls appropriate to your organization.
- Monitor changes to DNS zones, record sets, and role assignments.
- Treat registrar access as highly privileged because registrar-level nameserver changes can redirect the entire domain.

## Example target architecture

```text
Domain registrar
      |
      | NS delegation
      v
Azure DNS: example.com
      |
      +-- app.example.com            A/CNAME
      +-- www.example.com            CNAME
      +-- _acme-challenge.example.com TXT  <-- ACME automation

Automation identity
      |
      +-- Azure RBAC scoped to the DNS zone
```

---

Replace all example subscription IDs, resource groups, domains, addresses, identity IDs, name servers, and record values before using the commands in production.
