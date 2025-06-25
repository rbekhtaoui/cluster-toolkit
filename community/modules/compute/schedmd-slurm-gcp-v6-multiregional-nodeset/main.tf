# Copyright 2023 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

locals {
  # This label allows for billing report tracking based on module.
  labels = merge(var.labels, { ghpc_module = "schedmd-slurm-gcp-v6-nodeset", ghpc_role = "compute" })
}

module "gpu" {
  source = "../../../../modules/internal/gpu-definition"

  machine_type      = var.machine_type
  guest_accelerator = var.guest_accelerator
}

locals {
  guest_accelerator = module.gpu.guest_accelerator

  disable_automatic_updates_metadata = var.allow_automatic_updates ? {} : { google_disable_automatic_updates = "TRUE" }

  metadata = merge(
    local.disable_automatic_updates_metadata,
    var.metadata
  )

  name = substr(replace(var.name, "/[^a-z0-9]/", ""), 0, 14)

  additional_disks = [
    for ad in var.additional_disks : {
      disk_name    = ad.disk_name
      device_name  = ad.device_name
      disk_type    = ad.disk_type
      disk_size_gb = ad.disk_size_gb
      disk_labels  = merge(ad.disk_labels, local.labels)
      auto_delete  = ad.auto_delete
      boot         = ad.boot
    }
  ]

  public_access_config = var.enable_public_ips ? [{ nat_ip = null, network_tier = null }] : []
  access_config        = length(var.access_config) == 0 ? local.public_access_config : var.access_config

  service_account = {
    email  = var.service_account_email
    scopes = var.service_account_scopes
  }

  ghpc_startup_script = [{
    filename = "ghpc_nodeset_startup.sh"
    content  = var.startup_script
  }]

  region_subnet_map = {
    for key, self_link in var.subnetworks_self_link :
    split("/", self_link)[8] => self_link
  }
  multiregional_nodeset = {
    node_count_static      = var.node_count_static
    node_count_dynamic_max = var.node_count_dynamic_max
    node_conf              = var.node_conf
    nodeset_name           = local.name
    dws_flex               = var.dws_flex

    disk_auto_delete = var.disk_auto_delete
    disk_labels      = merge(local.labels, var.disk_labels)
    disk_size_gb     = var.disk_size_gb
    disk_type        = var.disk_type
    additional_disks = local.additional_disks

    bandwidth_tier = var.bandwidth_tier
    can_ip_forward = var.can_ip_forward

    enable_confidential_vm = var.enable_confidential_vm
    enable_placement       = var.enable_placement
    placement_max_distance = var.placement_max_distance
    enable_oslogin         = var.enable_oslogin
    enable_shielded_vm     = var.enable_shielded_vm
    gpu                    = one(local.guest_accelerator)

    labels                    = local.labels
    machine_type              = terraform_data.machine_type_zone_validation.output
    advanced_machine_features = var.advanced_machine_features
    metadata                  = local.metadata
    min_cpu_platform          = var.min_cpu_platform

    on_host_maintenance      = var.on_host_maintenance
    preemptible              = var.preemptible
    regions                  = [for region in var.regions_info : region.name]
    service_account          = local.service_account
    shielded_instance_config = var.shielded_instance_config
    source_image_family      = local.source_image_family             # requires source_image_logic.tf
    source_image_project     = local.source_image_project_normalized # requires source_image_logic.tf
    source_image             = local.source_image                    # requires source_image_logic.tf
    subnetworks_self_link    = local.region_subnet_map
    additional_networks      = var.additional_networks
    access_config            = local.access_config
    tags                     = var.tags
    spot                     = var.enable_spot_vm
    termination_action       = try(var.spot_instance_config.termination_action, null)
    maintenance_interval     = var.maintenance_interval
    instance_properties_json = jsonencode(var.instance_properties)

    zone_target_shape = var.zone_target_shape
    zone_policy_allow = {
      for region in var.regions_info :
      region.name => region.zones
    }
    zone_policy_deny = local.zones_deny

    startup_script  = local.ghpc_startup_script
    network_storage = var.network_storage

    enable_opportunistic_maintenance = var.enable_opportunistic_maintenance
  }
}

locals {
  zones_deny = {
    for region in var.regions_info :
    region.name => setsubtract(
      data.google_compute_zones.available[region.name].names,
      region.zones
    )
  }
}

data "google_compute_zones" "available" {
  for_each = {
    for region in var.regions_info : region.name => region
  }
  project = var.project_id
  region  = each.value.name

  lifecycle {
    postcondition {
      condition     = length(setsubtract(each.value.zones, self.names)) == 0
      error_message = <<-EOD
      Invalid zones for region "${each.value.name}" = ${jsonencode(setsubtract(each.value.zones, self.names))}
      Available zones=${jsonencode(self.names)}
      EOD
    }
  }
}


data "google_compute_machine_types" "machine_types_by_zone" {
  for_each = toset(flatten([for region in var.regions_info : region.zones]))
  project  = var.project_id
  filter   = format("name = \"%s\"", var.machine_type)
  zone     = each.value
}

locals {
  machine_types_by_zone   = data.google_compute_machine_types.machine_types_by_zone
  zones_with_machine_type = [for k, v in local.machine_types_by_zone : k if length(v.machine_types) > 0]
}

resource "terraform_data" "machine_type_zone_validation" {
  input = var.machine_type
  lifecycle {
    precondition {
      condition     = length(local.zones_with_machine_type) > 0
      error_message = <<-EOT
        machine type ${var.machine_type} is not available in any of the zones". To list zones in which it is available, run:

        gcloud compute machine-types list --filter="name=${var.machine_type}"
        EOT
    }
  }
}
