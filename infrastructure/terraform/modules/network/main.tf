# Subnet plan inside the /16:
#   nodes   -> .1.0/24    AKS worker node primary VNICs
#   pods    -> .16.0/20   AKS pod IPs (Azure CNI Overlay attaches pod NICs here)
#   mew     -> .32.0/28   Postgres Flexible Server delegated subnet
#   apps_lb -> .0.0/26    Public load balancers for ingress (small range — LB
#                          consumes one IP per Service of type LoadBalancer)
locals {
  cidr_lb    = cidrsubnet(var.vnet_cidr, 10, 0)
  cidr_nodes = cidrsubnet(var.vnet_cidr, 8, 1)
  cidr_pods  = cidrsubnet(var.vnet_cidr, 4, 1)
  cidr_mew   = cidrsubnet(var.vnet_cidr, 12, 512)
}

resource "azurerm_virtual_network" "this" {
  name                = "${var.name_prefix}-vnet"
  resource_group_name = var.resource_group_name
  location            = var.region
  address_space       = [var.vnet_cidr]
  tags                = var.tags
}

resource "azurerm_subnet" "nodes" {
  name                 = "${var.name_prefix}-subnet-nodes"
  resource_group_name  = var.resource_group_name
  virtual_network_name = azurerm_virtual_network.this.name
  address_prefixes     = [local.cidr_nodes]

  # Outbound to Service Bus (Event Hubs), Key Vault, Storage stays on the
  # Microsoft backbone via service endpoints — no NAT egress cost.
  service_endpoints = [
    "Microsoft.KeyVault",
    "Microsoft.Storage",
    "Microsoft.EventHub",
  ]
}

resource "azurerm_subnet" "pods" {
  name                 = "${var.name_prefix}-subnet-pods"
  resource_group_name  = var.resource_group_name
  virtual_network_name = azurerm_virtual_network.this.name
  address_prefixes     = [local.cidr_pods]

  # AKS attaches this delegation automatically on first cluster create.
  # Declared here so Terraform owns the value and does not propose to remove it.
  delegation {
    name = "aks-delegation"
    service_delegation {
      name    = "Microsoft.ContainerService/managedClusters"
      actions = ["Microsoft.Network/virtualNetworks/subnets/join/action"]
    }
  }
}

resource "azurerm_subnet" "lb" {
  name                 = "${var.name_prefix}-subnet-lb"
  resource_group_name  = var.resource_group_name
  virtual_network_name = azurerm_virtual_network.this.name
  address_prefixes     = [local.cidr_lb]
}

# Postgres Flexible Server requires a delegated subnet. Once delegated, no
# other resource can use this subnet. The dedicated /28 is the minimum size.
resource "azurerm_subnet" "mew" {
  name                 = "${var.name_prefix}-subnet-mew"
  resource_group_name  = var.resource_group_name
  virtual_network_name = azurerm_virtual_network.this.name
  address_prefixes     = [local.cidr_mew]

  # Postgres Flexible Server auto-attaches this service endpoint on first
  # server create (used internally for backup traffic). Declared here so
  # Terraform owns the value and does not propose to remove it.
  service_endpoints = ["Microsoft.Storage"]

  delegation {
    name = "postgres-flexible-server"
    service_delegation {
      name    = "Microsoft.DBforPostgreSQL/flexibleServers"
      actions = ["Microsoft.Network/virtualNetworks/subnets/join/action"]
    }
  }
}

# Private DNS zone Flexible Server registers itself in. AKS pods resolving
# the Mew FQDN inside the VNet get the private IP because of this zone.
resource "azurerm_private_dns_zone" "postgres" {
  name                = "${var.name_prefix}.private.postgres.database.azure.com"
  resource_group_name = var.resource_group_name
  tags                = var.tags
}

resource "azurerm_private_dns_zone_virtual_network_link" "postgres" {
  name                  = "${var.name_prefix}-pg-dns-link"
  resource_group_name   = var.resource_group_name
  private_dns_zone_name = azurerm_private_dns_zone.postgres.name
  virtual_network_id    = azurerm_virtual_network.this.id
  registration_enabled  = false
  tags                  = var.tags
}

# NSG: AKS worker nodes. Allow same-subnet (pod-to-pod + node-to-node), allow
# all egress (Microsoft backbone via service endpoints handles managed-service
# traffic). Postgres ingress is controlled by Flexible Server's own firewall
# + the delegated subnet.
resource "azurerm_network_security_group" "nodes" {
  name                = "${var.name_prefix}-nsg-nodes"
  resource_group_name = var.resource_group_name
  location            = var.region
  tags                = var.tags

  security_rule {
    name                       = "AllowVNetInbound"
    priority                   = 100
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "*"
    source_address_prefix      = "VirtualNetwork"
    source_port_range          = "*"
    destination_address_prefix = "VirtualNetwork"
    destination_port_range     = "*"
  }

  security_rule {
    name                       = "AllowAzureLoadBalancer"
    priority                   = 110
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "*"
    source_address_prefix      = "AzureLoadBalancer"
    source_port_range          = "*"
    destination_address_prefix = "*"
    destination_port_range     = "*"
  }

  security_rule {
    name                       = "DenyAllInbound"
    priority                   = 4096
    direction                  = "Inbound"
    access                     = "Deny"
    protocol                   = "*"
    source_address_prefix      = "*"
    source_port_range          = "*"
    destination_address_prefix = "*"
    destination_port_range     = "*"
  }
}

resource "azurerm_subnet_network_security_group_association" "nodes" {
  subnet_id                 = azurerm_subnet.nodes.id
  network_security_group_id = azurerm_network_security_group.nodes.id
}
