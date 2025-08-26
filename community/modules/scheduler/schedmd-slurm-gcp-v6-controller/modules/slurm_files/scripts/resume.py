#!/slurm/python/venv/bin/python3.13

# Copyright (C) SchedMD LLC.
# Copyright 2015 Google Inc. All rights reserved.
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

from typing import List, Optional, Dict, Any
import argparse
from datetime import timedelta
import shlex
import json
import logging
import os
import yaml
import collections
from pathlib import Path
from dataclasses import dataclass
from addict import Dict as NSDict # type: ignore
from concurrent.futures import ThreadPoolExecutor, as_completed
import time

import util
from util import (
    chunked,
    ensure_execute,
    execute_with_futures,
    log_api_request,
    map_with_futures,
    run,
    separate,
    to_hostlist,
    trim_self_link,
    wait_for_operation,
)
from util import lookup, ReservationDetails
import tpu
import mig_flex

log = logging.getLogger()

PLACEMENT_MAX_CNT = 1500
# Placement group needs to be the same for an entire bulk_insert hence
# if placement is used the actual BULK_INSERT_LIMIT will be
# max([1000, PLACEMENT_MAX_CNT])
BULK_INSERT_LIMIT = 5000

# https://cloud.google.com/compute/docs/instance-groups#types_of_managed_instance_groups
ZONAL_MIG_SIZE_LIMIT = 1000
MAX_FAILOVER_TIMEOUT = 300

@dataclass(frozen=True)
class ResumeJobData:
    job_id: int
    partition: str
    nodes_alloc: List[str]

@dataclass(frozen=True)
class ResumeData:
    jobs: List[ResumeJobData]


def get_resume_file_data() -> Optional[ResumeData]:
    if not (path := os.getenv("SLURM_RESUME_FILE")):
        log.error("SLURM_RESUME_FILE was not in environment. Cannot get detailed job, node, partition allocation data.")
        return None
    blob = Path(path).read_text()
    log.debug(f"Resume data: {blob}")
    data = json.loads(blob)

    jobs = []
    for jo in data.get("jobs", []):
        job = ResumeJobData(
            job_id = jo.get("job_id"),
            partition = jo.get("partition"),
            nodes_alloc = util.to_hostnames(jo.get("nodes_alloc")),
        )
        jobs.append(job)
    return ResumeData(jobs=jobs)

def instance_properties(nodeset: NSDict, model:str, placement_group:Optional[str], labels:Optional[dict], job_id:Optional[int]):
    props = NSDict()

    if labels: # merge in extra labels on instance and disks
        template_link = lookup().node_template(model)
        template_info = lookup().template_info(template_link)

        props.labels = {**template_info.labels, **labels}
        
        for disk in template_info.disks:
            if disk.initializeParams.get("diskType", "local-ssd") == "local-ssd":
                continue # do not label local ssd
            disk.initializeParams.labels.update(labels)
        props.disks = template_info.disks

    if placement_group:
        props.resourcePolicies = [placement_group]

    if reservation := lookup().nodeset_reservation(nodeset):
        update_reservation_props(reservation, props, placement_group, reservation.calendar)

    if (fr := lookup().future_reservation(nodeset)) and fr.specific:
        assert fr.active_reservation
        update_reservation_props(fr.active_reservation, props, placement_group, fr.calendar)

    if props.resourcePolicies:
       props.scheduling.onHostMaintenance = "TERMINATE"

    if nodeset.maintenance_interval:
        props.scheduling.maintenanceInterval = nodeset.maintenance_interval

    if nodeset.dws_flex.enabled and nodeset.dws_flex.use_bulk_insert:
        update_props_dws(props, nodeset.dws_flex, job_id)

    # Override with properties explicit specified in the nodeset
    props.update(nodeset.get("instance_properties") or {})
    return props

def update_reservation_props(reservation:ReservationDetails, props:NSDict, placement_group:Optional[str], calendar_mode:bool) -> None:
    props.reservationAffinity = {
        "consumeReservationType": "SPECIFIC_RESERVATION",
        "key": f"compute.{util.universe_domain()}/reservation-name",
        "values": [reservation.bulk_insert_name],
    }

    if reservation.dense or calendar_mode:
        props.scheduling.provisioningModel = "RESERVATION_BOUND"

    # Figure out `resourcePolicies`
    if reservation.policies: # use ones already attached to reservations
        props.resourcePolicies = reservation.policies
    elif reservation.dense and placement_group: # use once created by Slurm
        props.resourcePolicies = [placement_group]
    else: # vanilla reservations don't support external policies
        props.resourcePolicies = []
    log.info(
        f"reservation {reservation.bulk_insert_name} is being used with resourcePolicies: {props.resourcePolicies}")

def update_props_dws(props: NSDict, dws_flex: NSDict, job_id: Optional[int]) -> None:
    props.scheduling.onHostMaintenance = "TERMINATE"
    props.scheduling.instanceTerminationAction = "DELETE"
    props.reservationAffinity['consumeReservationType'] = "NO_RESERVATION"
    props.scheduling.maxRunDuration['seconds'] = dws_flex_duration(dws_flex, job_id)

def dws_flex_duration(dws_flex: NSDict, job_id: Optional[int]) -> int:
    max_duration = dws_flex.max_run_duration
    if dws_flex.use_job_duration and job_id is not None and (job := lookup().job(job_id)) and job.duration:
        if timedelta(seconds=30) <= job.duration <= timedelta(weeks=1):
            max_duration = int(job.duration.total_seconds())
        else:
            log.info("Job TimeLimit cannot be less than 30 seconds or exceed one week")
    return max_duration

def create_instances_request(nodes: List[str], placement_group: Optional[str], excl_job_id: Optional[int], region: Optional[str]= None):
    """Call regionInstances.bulkInsert to create instances"""
    assert 0 < len(nodes) <= BULK_INSERT_LIMIT

    # model here indicates any node that can be used to describe the rest
    model = next(iter(nodes))
    log.debug(f"create_instances_request: {model} placement: {placement_group}")

    nodeset = lookup().node_nodeset(model)
    template = lookup().node_template(model)
    labels = {"slurm_job_id": excl_job_id} if excl_job_id else None

    body = dict(
        count = len(nodes),
        sourceInstanceTemplate = template.get(region) if isinstance(template, dict) else template,
        # key is instance name, value overwrites properties (no overwrites)
        perInstanceProperties = {k: {} for k in nodes},
        instanceProperties = instance_properties(
            nodeset, model, placement_group, labels, excl_job_id
        ),
    )

    if placement_group and excl_job_id is not None:
        pass # do not set minCount to force "all or nothing" behavior
    else:
        body["minCount"] = 1

    zone_allow = (nodeset.zone_policy_allow.get(region) if isinstance(nodeset.zone_policy_allow, dict) else nodeset.zone_policy_allow) or []
    zone_deny = (nodeset.zone_policy_deny.get(region) if isinstance(nodeset.zone_policy_deny, dict) else nodeset.zone_policy_deny) or []

    if len(zone_allow) == 1 : # if only one zone is used, use zonal BulkInsert API, as less prone to errors
        api_method = lookup().compute.instances().bulkInsert
        method_args = {"zone": zone_allow[0]}
    else:
        api_method = lookup().compute.regionInstances().bulkInsert
        method_args = {"region": lookup().node_region(model) or region}
        
        body["locationPolicy"] = dict(
            locations = {
                **{ f"zones/{z}": {"preference": "ALLOW"} for z in zone_allow },
                **{ f"zones/{z}": {"preference": "DENY"} for z in zone_deny }},
            targetShape = nodeset.zone_target_shape,
        )
    
    req = api_method(
        project=lookup().project, 
        body=body, 
        **method_args)
    log.debug(f"new request: endpoint={req.methodId} nodes={to_hostlist(nodes)}")
    log_api_request(req)
    return req

def attempt_create_instances_in_region(group, chunk, region):
    """
    Prepare bulkInsert request for a specific region
    """
    try:
        bi_inserts = {}
        log.info(f"Attempting to create instances for group {group} in region {region}")

        bi_inserts[group] = create_instances_request(
            chunk.nodes, chunk.partition, chunk.placement_group, chunk.job_id, region
        )
        return bi_inserts
    except Exception as e:
        log.error(f"Error preparing bulk insert for group {group} in region {region}: {e}")
        raise

def attempt_create_with_failover(group, chunk, regions, resume_data):
    """
    Attempt to create instances with automatic failover across all regions
    Returns (success: bool, bulk_operations: dict)
    """
    log.info(f"Starting multi-region failover for group {group} across regions: {regions}")
    
    for region_index, region in enumerate(regions):
        try:
            log.info(f"Attempting to create instances for group {group} in region {region} (attempt {region_index + 1}/{len(regions)})")
            bi_inserts = {}
            
            bi_inserts_result = attempt_create_instances_in_region(group, chunk, region)
    
            try:
                bulk_ops = dict(
                    zip(bi_inserts.keys(), map_with_futures(ensure_execute, bi_inserts.values()))
                )
            except Exception as e:
                log.warning(f"Exception executing bulkInsert for group {group} in region {region}: {e}")
                continue
            
            started = {
                grp: op for grp, op in bulk_ops.items() if not isinstance(op, Exception)
            }
            failed = {
                grp: err for grp, err in bulk_ops.items() if isinstance(err, Exception)
            }
            
            if failed:
                failed_reqs = [f"{grp}: {str(err)}" for grp, err in failed.items()]
                log.warning(f"bulkInsert API failures in region {region}: {'; '.join(failed_reqs)}")
                
                if region_index == len(regions) - 1:
                    log.error(f"Last region {region} also failed for group {group}")
                    return False, {}
                else:
                    log.info(f"Trying next region for group {group}")
                    continue
            
            if started:
                log.info(f"bulkInsert successful for group {group} in region {region}")
                if log.isEnabledFor(logging.DEBUG):
                    for grp, op in started.items():
                        group_nodes = to_hostlist_fast(chunk.nodes)
                        name = op["name"]
                        gid = op["operationGroupId"]
                        log.debug(
                            f"new bulkInsert operation started: group={grp} nodes={group_nodes} name={name} operationGroupId={gid} region={region}"
                        )
                
                try:
                    bulk_operations = {grp: wait_for_operation(op) for grp, op in started.items()}
                    
                    successful_operations = {}
                    operation_errors = {}
                    
                    for grp, bulk_op in bulk_operations.items():
                        if "error" in bulk_op:
                            operation_errors[grp] = bulk_op["error"]
                        else:
                            successful_operations[grp] = bulk_op
                    
                    if operation_errors and not successful_operations:
                        log.warning(f"All bulkInsert operations failed in region {region}: {operation_errors}")
                        if region_index == len(regions) - 1:
                            log.error(f"Last region {region} operations failed for group {group}")
                            return False, {}
                        else:
                            continue
                    
                    log.info(f"bulkInsert operations completed for group {group} in region {region}")
                    return True, bulk_operations
                    
                except Exception as e:
                    log.warning(f"Exception waiting for operations in region {region}: {e}")
                    if region_index == len(regions) - 1:
                        return False, {}
                    else:
                        continue
            else:
                log.warning(f"No operations started for group {group} in region {region}")
                continue
                
        except Exception as e:
            log.warning(f"Unexpected exception for group {group} in region {region}: {e}")
            if region_index == len(regions) - 1:
                log.error(f"All regions exhausted for group {group}, last error: {e}")
                return False, {}
            else:
                continue
    
    log.error(f"All regions failed for group {group}")
    return False, {}

@dataclass()
class PlacementAndNodes:
    placement: Optional[str]
    nodes: List[str]

@dataclass(frozen=True)
class BulkChunk:
    nodes: List[str]
    prefix: str # <cluster_name>-<nodeset_name>
    chunk_idx: int
    excl_job_id: Optional[int]
    placement_group: Optional[str] = None

    @property
    def name(self):
        if self.placement_group is not None:
            return f"{self.prefix}:job{self.excl_job_id}:{self.placement_group}:{self.chunk_idx}"
        if self.excl_job_id is not None:
            return f"{self.prefix}:job{self.excl_job_id}:{self.chunk_idx}"
        return f"{self.prefix}:{self.chunk_idx}"
    

def group_nodes_bulk(nodes: List[str], resume_data: Optional[ResumeData], lkp: util.Lookup):
    """group nodes by nodeset, placement_group, exclusive_job_id if any"""
    if resume_data is None: # all nodes will be considered jobless
        resume_data = ResumeData(jobs=[])
        
    nodes_set = set(nodes) # turn into set to simplify intersection
    non_excl = nodes_set.copy()
    groups : Dict[Optional[int], List[PlacementAndNodes]] = {} # excl_job_id|none -> PlacementAndNodes

    # expand all exclusive job nodelists
    for job in resume_data.jobs:
        if not lkp.cfg.partitions[job.partition].enable_job_exclusive: 
            continue

        groups[job.job_id] = []
        # placement group assignment is based on all allocated nodes, ...
        for pn in create_placements(job.nodes_alloc, job.job_id, lkp):
            groups[job.job_id].append(
                PlacementAndNodes(
                    placement=pn.placement,
                    #... but we only want to handle nodes in nodes_resume in this run.
                    nodes = sorted(set(pn.nodes) & nodes_set)
                ))
        non_excl.difference_update(job.nodes_alloc)

    groups[None] = create_placements(sorted(non_excl), excl_job_id=None, lkp=lkp)

    def chunk_nodes(nodes: List[str]):
        if not nodes:
            return []
        
        model = nodes[0]
        
        if lkp.is_flex_node(model):
            chunk_size = ZONAL_MIG_SIZE_LIMIT
        elif lkp.node_is_tpu(model):
            ns_name = lkp.node_nodeset_name(model)
            chunk_size = tpu.TPU.make(ns_name, lkp).vmcount
        else:
            chunk_size = BULK_INSERT_LIMIT

        return chunked(nodes, n=chunk_size)
    
    chunks = [
        BulkChunk(
            nodes=nodes_chunk,
            prefix=lkp.node_prefix(nodes_chunk[0]), # <cluster_name>-<nodeset_name>
            excl_job_id = job_id,
            placement_group=pn.placement,
            chunk_idx=i)

        for job_id, placements in groups.items()
        for pn in placements if pn.nodes
        for i, nodes_chunk in enumerate(chunk_nodes(pn.nodes))
    ]
    return {chunk.name: chunk for chunk in chunks}


def resume_nodes(nodes: List[str], resume_data: Optional[ResumeData]):
    """resume nodes in nodelist"""
    lkp = lookup()
    # Prevent dormant nodes associated with a reservation from being resumed
    nodes, dormant_res_nodes = util.separate(lkp.is_dormant_res_node, nodes)
    
    if dormant_res_nodes:
        log.warning(f"Resume was unable to resume reservation nodes={dormant_res_nodes}")
        down_nodes_notify_jobs(dormant_res_nodes, "Reservation is not active, nodes cannot be resumed", resume_data)

    nodes, flex_managed = util.separate(lkp.is_provisioning_flex_node, nodes)
    if flex_managed:
        log.warning(f"Resume was unable to resume nodes={flex_managed} already managed by MIGs")
        down_nodes_notify_jobs(flex_managed, "VM is managed MIG, can not be resumed", resume_data)

    if not nodes:
        log.info("No nodes to resume")
        return

    nodes = sorted(nodes, key=lkp.node_prefix)
    grouped_nodes = group_nodes_bulk(nodes, resume_data, lkp)

    if log.isEnabledFor(logging.DEBUG):
        grouped_nodelists = {
            group: to_hostlist(chunk.nodes) for group, chunk in grouped_nodes.items()
        }
        log.debug(
            "node bulk groups: \n{}".format(yaml.safe_dump(grouped_nodelists).rstrip())
        )

    tpu_chunks, flex_chunks = [], []
    multi_region_groups = {}
    bi_inserts = {}
    regions = lkp.multiregional_regions(nodes)

    for group, chunk in grouped_nodes.items():
        model = chunk.nodes[0]

        if lkp.node_is_tpu(model):
            tpu_chunks.append(chunk.nodes)
        elif lkp.is_flex_node(model):
            flex_chunks.append(chunk)
        else:
            multi_region_groups[group] = chunk

    for chunk in flex_chunks:
        mig_flex.resume_flex_chunk(chunk.nodes, chunk.excl_job_id, lkp)

    bulk_ops = {}

    for group, chunk in multi_region_groups.items():
        try:
            log.info(f"Attempting multi-region resume for group={group} regions={chunk.regions}")
            success, op = attempt_create_with_failover_parallel(group, chunk, chunk.regions, resume_data)
            if success:
                bulk_ops[group] = op
            else:
                log.error(f"Failed to resume nodes for group={group} in all regions")
                down_nodes_notify_jobs(chunk.nodes, "Failed to create nodes in all regions", resume_data)

        except Exception as e:
            log.error(f"Error resuming nodes for group={group}: {e}")
            down_nodes_notify_jobs(chunk.nodes, f"GCP Error: {str(e)}", resume_data)
    
    if log.isEnabledFor(logging.DEBUG):
        for group, op in bulk_ops.items():
            if isinstance(op, dict):
                name = op.get("name")
                gid = op.get("operationGroupId")
                log.debug(f"new bulkInsert operation started: group={group} nodes={grouped_nodelists[group]} name={name} operationGroupId={gid}")

    # wait for all bulkInserts to complete and log any errors
    bulk_operations = {}
    for group, op in bulk_ops.items():
        if isinstance(op, dict): 
            bulk_operations[group] = wait_for_operation(op)

    # Start TPU after regular nodes so that regular nodes are not affected by the slower TPU nodes
    execute_with_futures(tpu.start_tpu, tpu_chunks)

    for group, op in bulk_operations.items():
        _handle_bulk_insert_op(op, grouped_nodes[group].nodes, resume_data)
        

def _get_failed_zonal_instance_inserts(bulk_op: Any, zone: str, lkp: util.Lookup) -> list[Any]:
    group_id = bulk_op["operationGroupId"]
    user = bulk_op["user"]
    started = bulk_op["startTime"]
    ended = bulk_op["endTime"]
   
    fltr = f'(user eq "{user}") AND (operationType eq "insert") AND (creationTimestamp > "{started}") AND (creationTimestamp < "{ended}")'
    act = lkp.compute.zoneOperations()
    req = act.list(project=lkp.project, zone=zone, filter=fltr)
    ops = []
    while req is not None:
        result = util.ensure_execute(req)
        for op in result.get("items", []):
            if op.get("operationGroupId") == group_id and "error" in op:
                ops.append(op)
        req = act.list_next(req, result)
    return ops


def _get_failed_instance_inserts(bulk_op: Any, lkp: util.Lookup) -> list[Any]:
    zones = set() # gather zones that had failed inserts
    for loc, stat in bulk_op.get("instancesBulkInsertOperationMetadata", {}).get("perLocationStatus", {}).items():
        pref, zone = loc.split("/", 1)
        if not pref == "zones":
            log.error(f"Unexpected location: {loc} in operation {bulk_op['name']}")
            continue
        if stat.get("targetVmCount", 0) !=  stat.get("createdVmCount", 0):
            zones.add(zone)
    
    res = []
    for zone in zones:
        res.extend(_get_failed_zonal_instance_inserts(bulk_op, zone, lkp))
    return res

def _handle_bulk_insert_op(op: Dict, nodes: List[str], resume_data: Optional[ResumeData]) -> None:
    """
    Handles **DONE** BulkInsert operations
    """
    assert op["operationType"] == "bulkInsert" and op["status"] == "DONE", f"unexpected op: {op}"

    group_id = op["operationGroupId"]
    if "error" in op:
        error = op["error"]["errors"][0]
        log.error(
            f"bulkInsert operation error: {error['code']} name={op['name']} operationGroupId={group_id} nodes={to_hostlist(nodes)}"
        )
    
    created = 0
    for status in op["instancesBulkInsertOperationMetadata"]["perLocationStatus"].values():
        created += status.get("createdVmCount", 0)
    if created == len(nodes):
        log.info(f"created {len(nodes)} instances: nodes={to_hostlist(nodes)}")
        return # no need to gather status of insert-operations.

    # TODO: don't gather insert-operations per bulkInsert request, instead aggregate it
    #  across all bulkInserts (goes one level above this function) 
    failed = _get_failed_instance_inserts(op, util.lookup())
    
    # Multiple errors are possible, group by all of them (joined string codes)
    by_error_inserts = util.groupby_unsorted(
        failed,
        lambda op: "+".join(err["code"] for err in op["error"]["errors"]),
    )
    for code, failed_ops in by_error_inserts:
        failed_ops = list(failed_ops)
        failed_nodes = [trim_self_link(op["targetLink"]) for op in failed_ops]
        hostlist = util.to_hostlist(failed_nodes)
        log.error(
            f"{len(failed_nodes)} instances failed to start: {code} ({hostlist}) operationGroupId={group_id}"
        )

        msg = "; ".join(
            f"{err['code']}: {err['message'] if 'message' in err else 'no message'}"
            for err in failed_ops[0]["error"]["errors"]
        )
        if code != "RESOURCE_ALREADY_EXISTS":
            down_nodes_notify_jobs(failed_nodes, f"GCP Error: {msg}", resume_data)
        log.error(
            f"errors from insert for node '{failed_nodes[0]}' ({failed_ops[0]['name']}): {msg}"
        )

def attempt_create_with_failover_parallel(group, chunk, regions, resume_data):
    """
    Attempt to create instances in parallel across multiple regions.
    Returns (success: bool, bulk_operations: dict)
    """
    log.info(f"Starting multi-region parallel failover for group {group} across regions: {regions}")
    
    start_time = time.time()

    def create_in_region(region):
        try:
            log.info(f"[{region}] Preparing bulkInsert for group {group}")
            bi_req = create_instances_request(chunk.nodes, chunk.partition, chunk.placement_group, chunk.job_id, region)
            op = ensure_execute(bi_req)
            log.debug(f"[{region}] bulkInsert started for group {group}")
            completed = wait_for_operation(op)
            return region, completed
        except Exception as e:
            return region, e

    with ThreadPoolExecutor(max_workers=len(regions)) as executor:
        futures = {executor.submit(create_in_region, r): r for r in regions}

        for future in as_completed(futures, timeout=MAX_FAILOVER_TIMEOUT):
            region = futures[future]
            try:
                result = future.result()
                if not isinstance(result[1], Exception):
                    # Success -> cancel other futures
                    log.info(f"Success in region {region} for group {group}")
                    for f in futures:
                        f.cancel()
                    return True, {group: result[1]}
                else:
                    log.warning(f"[{region}] Failed to create instances: {result[1]}")
            except Exception as e:
                log.error(f"[{region}] Exception during failover: {e}")

    elapsed = time.time() - start_time
    log.error(f"All regions failed for group {group} after {elapsed:.2f}s")
    return False, {}

def down_nodes_notify_jobs(nodes: List[str], reason: str, resume_data: Optional[ResumeData]) -> None:
    """set nodes down with reason"""
    nodes_set = set(nodes) # turn into set to speed up intersection
    jobs = resume_data.jobs if resume_data else []
    reason_quoted = shlex.quote(reason)

    for job in jobs:
        if not (set(job.nodes_alloc) & nodes_set):
            continue
        run(f"{lookup().scontrol} update jobid={job.job_id} admincomment={reason_quoted}", check=False)
        run(f"{lookup().scontrol} notify {job.job_id} {reason_quoted}", check=False)

    nodelist = util.to_hostlist(nodes)
    log.error(f"Marking nodes {nodelist} as DOWN, reason: {reason}")
    run(f"{lookup().scontrol} update nodename={nodelist} state=down reason={reason_quoted}", check=False)
    
    


def create_placement_request(pg_name: str, region: str, max_distance: Optional[int], accelerator_topology: Optional[str]):
    config = {
        "name": pg_name,
        "region": region,
        "groupPlacementPolicy": {
            "collocation": "COLLOCATED",
            "maxDistance": max_distance,
            "gpuTopology": accelerator_topology,
        },
    }
    
    request = lookup().compute.resourcePolicies().insert(
        project=lookup().project, region=region, body=config
    )
    log_api_request(request)
    return request


def create_placements(nodes: List[str], excl_job_id:Optional[int], lkp: util.Lookup) -> List[PlacementAndNodes]:
    nodeset_map = collections.defaultdict(list)
    for node in nodes: # split nodes on nodesets
        nodeset_map[lkp.node_nodeset_name(node)].append(node)

    placements = []
    for _, ns_nodes in nodeset_map.items():
        placements.extend(create_nodeset_placements(ns_nodes, excl_job_id, lkp))
    return placements


def _allocate_nodes_to_placements(nodes: List[str], excl_job_id:Optional[int], lkp: util.Lookup) -> List[PlacementAndNodes]:
    # canned result for no placement policies created
    no_pp = [PlacementAndNodes(placement=None, nodes=nodes)]

    model = nodes[0]
    nodeset = lkp.node_nodeset(model)

    is_slice = bool(getattr(nodeset, 'accelerator_topology', None))

    excl_job_placement = (excl_job_id is not None) and (not is_slice)
    
    if excl_job_placement and len(nodes) < 2:
        return no_pp # don't create placement_policy for just one node

    if lkp.is_flex_node(model):
        return no_pp # TODO(FLEX): Add support for workload policies 
    if lkp.node_is_tpu(model):
        return no_pp
    if not (nodeset.enable_placement and valid_placement_node(model)):
        return no_pp
    
    max_count = calculate_chunk_size(nodeset, lkp)

    name_prefix = f"{lkp.cfg.slurm_cluster_name}-slurmgcp-managed-{nodeset.nodeset_name}"
   
    if excl_job_placement: # simply chunk given nodes by max size of placement
        return [
            PlacementAndNodes(placement=f"{name_prefix}-{excl_job_id}-{i}", nodes=chunk)
            for i, chunk in enumerate(chunked(nodes, n=max_count))
        ]

    # split whole nodeset (not only nodes to resume) into chunks of max size of placement
    # create placements (most likely already exists) placements for requested nodes
    chunks = collections.defaultdict(list) # chunk_id -> nodes
    invalid = []

    for node in nodes:
        try:
            chunk = lkp.node_index(node) // max_count
            chunks[chunk].append(node)
        except:
            invalid.append(node)
    
    placements = [
        # NOTE: use 0 instead of job_id for consistency with previous SlurmGCP behavior
        PlacementAndNodes(placement=f"{name_prefix}-0-{c_id}", nodes=c_nodes) 
        for c_id, c_nodes in chunks.items() 
    ]

    if invalid:
        placements.append(PlacementAndNodes(placement=None, nodes=invalid))
        log.error(f"Could not find placement for nodes with unexpected names: {to_hostlist(invalid)}")

    return placements

def calculate_hosts_per_topo(accelerator_topology: str, machine_type: NSDict) -> int:
    # Calculate total number of hosts per topology (Assumes format: '1x72')
    try:
        top_split = [int(x) for x in accelerator_topology.split("x")]
    except Exception as e:
        log.error(f"Accelerator topology {accelerator_topology} is formatted incorrectly.")
        raise e

    if len(machine_type.accelerators) == 0:
        gpus_per_machine = 0
    else: 
        gpus_per_machine = machine_type.accelerators[0].count

    if len(top_split) != 2:
        log.error(f"Accelerator topology {accelerator_topology} is formatted incorrectly.")
    elif top_split[0] <= 0 or top_split[1] <= 0:
        log.error(f"Accelerator topology {accelerator_topology} is formatted incorrectly.")
    elif gpus_per_machine <= 0:
        log.error(f"The machine type has no accelerators. Cannot use accelerator topology {accelerator_topology}.")
    elif top_split[1] % gpus_per_machine:
        log.error(f"The GPU count {gpus_per_machine} per node is not a factor of the accelerator topology {accelerator_topology}")
    
    return (top_split[0] * top_split[1]) // gpus_per_machine
 
def calculate_chunk_size(nodeset: NSDict, lkp: util.Lookup) -> int:
    # Calculates the chunk size based on max distance value received or accelerator topology
    # Assuming nodeset is not tpu
    machine_type = lkp.template_info(nodeset.instance_template).machine_type
    max_distance = nodeset.placement_max_distance
    accelerator_topology = nodeset.accelerator_topology

    # Look for accelerator topology first
    if accelerator_topology:
        hosts_per_topo = calculate_hosts_per_topo(accelerator_topology, machine_type)
        return hosts_per_topo

    if max_distance == 1:
        return 22
    elif max_distance == 2:
        if machine_type.family.startswith("a3"):
            return 256
        else:
            return 150
    elif max_distance == 3:
        return 1500
    else:
        return PLACEMENT_MAX_CNT

def create_nodeset_placements(nodes: List[str], excl_job_id:Optional[int], lkp: util.Lookup) -> List[PlacementAndNodes]:    
    placements = _allocate_nodes_to_placements(nodes, excl_job_id, lkp)
    region = lkp.node_region(nodes[0])
    max_distance = lkp.node_nodeset(nodes[0]).get('placement_max_distance')
    accelerator_topology = lkp.nodeset_accelerator_topology(lkp.node_nodeset_name(nodes[0]))

    if log.isEnabledFor(logging.DEBUG):
        debug_p = {p.placement: to_hostlist(p.nodes) for p in placements}
        log.debug(
            f"creating {len(placements)} placement groups: \n{yaml.safe_dump(debug_p).rstrip()}"
        )

    requests = {
        p.placement: create_placement_request(p.placement, region, max_distance, accelerator_topology) for p in placements if p.placement
    }
    if not requests:
        return placements
    # TODO: aggregate all requests for whole resume and execute them at once (don't limit to nodeset/job)
    ops = dict(
        zip(requests.keys(), map_with_futures(ensure_execute, requests.values()))
    )

    def classify_result(item):
        op = item[1]
        if not isinstance(op, Exception):
            return "submitted"
        if all(e.get("reason") == "alreadyExists" for e in op.error_details): # type: ignore
            return "redundant"
        return "failed"

    grouped_ops = dict(util.groupby_unsorted(list(ops.items()), classify_result))
    submitted, redundant, failed = (
        dict(grouped_ops.get(key, {})) for key in ("submitted", "redundant", "failed")
    )
    if redundant:
        log.warning(
            "placement policies already exist: {}".format(",".join(redundant.keys()))
        )
    if failed:
        reqs = [f"{e}" for _, e in failed.values()]
        log.fatal("failed to create placement policies: {}".format("; ".join(reqs)))
    operations = {group: wait_for_operation(op) for group, op in submitted.items()}
    for group, op in operations.items():
        if "error" in op:
            msg = "; ".join(
                f"{err['code']}: {err['message'] if 'message' in err else 'no message'}"
                for err in op["error"]["errors"]
            )
            log.error(
                f"placement group failed to create: '{group}' ({op['name']}): {msg}"
            )

    log.info(
        f"created {len(operations)} placement groups ({to_hostlist(operations.keys())})"
    )
    return placements


def valid_placement_node(node: str) -> bool:
    invalid_types = frozenset(["e2", "t2d", "n1", "t2a", "m1", "m2", "m3"])
    mt = lookup().node_template_info(node).machineType
    if mt.split("-")[0] in invalid_types:
        log.warn(f"Unsupported machine type for placement policy: {mt}.")
        log.warn(
            f"Please do not use any the following machine types with placement policy: ({','.join(invalid_types)})"
        )
        return False
    return True


def main(nodelist: str) -> None:
    """main called when run as script"""
    log.debug(f"ResumeProgram {nodelist}")
    # Filter out nodes not in config.yaml
    other_nodes, nodes = separate(
        lookup().is_power_managed_node, util.to_hostnames(nodelist)
    )
    if other_nodes:
        log.error(
            f"Ignoring non-power-managed nodes '{to_hostlist(other_nodes)}' from '{nodelist}'"
        )

    if not nodes:
        log.info("No nodes to resume")
        return
    resume_data = get_resume_file_data()
    log.info(f"resume {util.to_hostlist(nodes)}")
    resume_nodes(nodes, resume_data)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("nodelist", help="list of nodes to resume")
    args = util.init_log_and_parse(parser)
    main(args.nodelist)
