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

output "multiregional_nodeset" {
  description = "Details of the nodeset. Typically used as input to `schedmd-slurm-gcp-v6-partition`."
  value       = local.multiregional_nodeset

  precondition {
    condition = !contains([
      "c3-:pd-standard",
      "h3-:pd-standard",
      "h3-:pd-ssd",
    ], "${substr(var.machine_type, 0, 3)}:${var.disk_type}")
    error_message = "A disk_type=${var.disk_type} cannot be used with machine_type=${var.machine_type}."
  }

  precondition {
    condition     = var.placement_max_distance == null || var.enable_placement
    error_message = "placement_max_distance requires enable_placement to be set to true."
  }

  precondition {
    condition     = !(startswith(var.machine_type, "a3-") && var.placement_max_distance == 1)
    error_message = "A3 machines do not support a placement_max_distance of 1."
  }

  precondition {
    condition     = !var.enable_placement || !var.dws_flex.enabled
    error_message = "Cannot use DWS Flex with `enable_placement`."
  }

  precondition {
    condition     = var.node_count_dynamic_max > 0 || var.node_count_static > 0
    error_message = <<-EOD
      This nodeset contains zero nodes, there should be at least one static or dynamic node
    EOD
  }
}
