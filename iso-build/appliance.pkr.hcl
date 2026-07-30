# Packer template: builds a ready-to-run ACME/GlobalProtect appliance VM
# image headlessly, using the same ks.cfg as build-iso.sh.
#
# Requirements (on your OWN build machine):
#   - Packer >= 1.9:        https://developer.hashicorp.com/packer/install
#   - QEMU/KVM:             sudo dnf install -y qemu-kvm
#   - packer-plugin-qemu:   packer plugins install github.com/hashicorp/qemu
#
# Usage:
#   packer init appliance.pkr.hcl
#   packer build \
#     -var 'iso_url=/path/to/Rocky-9-x86_64-minimal.iso' \
#     -var 'iso_checksum=sha256:xxxxxxxx...' \
#     appliance.pkr.hcl
#
# Output: output-acme-appliance/acme-appliance.qcow2

packer {
  required_plugins {
    qemu = {
      version = ">= 1.1.0"
      source  = "github.com/hashicorp/qemu"
    }
  }
}

variable "iso_url" {
  type        = string
  description = "Path or URL to a Rocky Linux 9 minimal ISO"
}

variable "iso_checksum" {
  type        = string
  description = "Checksum of the ISO, e.g. sha256:xxxx..."
}

variable "vm_name" {
  type    = string
  default = "acme-appliance"
}

variable "disk_size_mb" {
  type    = number
  default = 20480
}

variable "memory_mb" {
  type    = number
  default = 2048
}

variable "cpus" {
  type    = number
  default = 2
}

source "qemu" "acme_appliance" {
  iso_url      = var.iso_url
  iso_checksum = var.iso_checksum

  vm_name      = "${var.vm_name}.qcow2"
  output_directory = "output-${var.vm_name}"
  disk_size    = var.disk_size_mb
  memory       = var.memory_mb
  cpus         = var.cpus
  format       = "qcow2"
  accelerator  = "kvm"
  headless     = true

  http_directory = "."

  boot_wait = "10s"
  boot_command = [
    "<up><wait><tab>",
    " inst.ks=http://{{ .HTTPIP }}:{{ .HTTPPort }}/ks.cfg",
    " inst.repo=cdrom",
    "<enter>"
  ]

  ssh_username = "root"
  ssh_timeout  = "45m"
  shutdown_command = "shutdown -P now"
}

build {
  sources = ["source.qemu.acme_appliance"]

  provisioner "shell" {
    inline = [
      "systemctl is-enabled acme-webui.service",
      "systemctl is-enabled acme-renew.timer",
      "test -f /root/ks-post.log && tail -n 40 /root/ks-post.log || true",
    ]
  }
}

# ---------------------------------------------------------------------
# Direct-to-vCenter alternative (uncomment and adjust):
#
# source "vsphere-iso" "acme_appliance" {
#   vcenter_server      = "vcenter.example.com"
#   username            = "svc-packer@vsphere.local"
#   password            = "CHANGE-ME"
#   insecure_connection = true
#
#   cluster        = "YOUR-CLUSTER"
#   datastore      = "YOUR-DATASTORE"
#   network        = "YOUR-PORTGROUP"
#   folder         = "Appliances"
#   vm_name        = var.vm_name
#   guest_os_type  = "rhel9_64Guest"
#
#   CPUs      = var.cpus
#   RAM       = var.memory_mb
#   disk_controller_type = ["pvscsi"]
#   storage {
#     disk_size             = var.disk_size_mb
#     disk_thin_provisioned = true
#   }
#
#   iso_paths = ["[YOUR-DATASTORE] ISOs/Rocky-9-x86_64-minimal.iso"]
#   http_directory = "."
#   boot_command = [
#     "<up><wait><tab>",
#     " inst.ks=http://{{ .HTTPIP }}:{{ .HTTPPort }}/ks.cfg",
#     " inst.repo=cdrom",
#     "<enter>"
#   ]
#   ssh_username = "root"
#   convert_to_template = true
# }
# ---------------------------------------------------------------------
