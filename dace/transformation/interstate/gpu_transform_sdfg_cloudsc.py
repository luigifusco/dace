# Copyright 2019-2023 ETH Zurich and the DaCe authors. All rights reserved.
""" Contains inter-state transformations of an SDFG to run on the GPU. """

from dace import data, memlet, dtypes, registry, sdfg as sd, symbolic
from dace.sdfg import nodes, scope, graph
from dace.sdfg import utils as sdutil
from dace.transformation import transformation, helpers as xfh
from dace.transformation.pass_pipeline import Pipeline
from dace.transformation.passes.loop_info import Loops, LoopInfo
from dace.properties import Property, make_properties

from collections import defaultdict
from copy import deepcopy as dc
from enum import Enum
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple


gpu_storage = [dtypes.StorageType.GPU_Global, dtypes.StorageType.GPU_Shared, dtypes.StorageType.CPU_Pinned]


def _recursive_check(node, state, gpu_scalars, is_out: bool):
    """
    Recursively checks if the outputs or inputs of a node are scalars and if they are/should be stored in GPU memory.
    """
    scalset = set()
    scalout = True
    sdfg = state.parent
    if is_out:
        iter_function = state.out_edges
        path_index = -1
    else:
        iter_function = state.in_edges
        path_index = 0
    for e in iter_function(node):
        last_edge = state.memlet_path(e)[path_index]
        if not isinstance(last_edge.dst, nodes.AccessNode):
            continue
        desc = sdfg.arrays[last_edge.dst.data]
        if isinstance(desc, data.Scalar):
            if desc.storage in gpu_storage or last_edge.dst.data in gpu_scalars:
                scalout = False
            scalset.add(last_edge.dst.data)
            sset, ssout = _recursive_check(last_edge.dst, state, gpu_scalars, is_out)
            scalset = scalset.union(sset)
            scalout = scalout and ssout
        elif desc.storage not in gpu_storage and last_edge.data.num_elements() == 1:
            sset, ssout = _recursive_check(last_edge.dst, state, gpu_scalars, is_out)
            scalset = scalset.union(sset)
            scalout = scalout and ssout
        else:
            scalout = False
    return scalset, scalout

def recursive_out_check(node, state, gpu_scalars):
    return _recursive_check(node, state, gpu_scalars, True)

def recursive_in_check(node, state, gpu_scalars):
    return _recursive_check(node, state, gpu_scalars, False)


def _codenode_condition(node):
    return isinstance(node, (nodes.LibraryNode, nodes.NestedSDFG)) and node.schedule == dtypes.ScheduleType.GPU_Default

class ArrayStatus(Enum):
    """
        At the moment, we ignore a scenario where we work on a nested SDFG,
        and the array has a CPU copy in the outer SDFG.
    """
    CPU_ONLY = 0
    GPU_ONLY = 1
    CPU_AND_GPU_OUTER_SDFG = 2
    CPU_AND_GPU_THIS_SDFG = 3

@dataclass
class ArrayType:
    cpu: Optional[str]
    gpu: Optional[str]
    status: ArrayStatus

class SDFGArrays:

    def __init__(self, sdfg: sd.SDFG):
        self._arrays = {}
        self._sdfg = sdfg

    def move_to_gpu(self, name: str) -> str:
        desc = self._sdfg.arrays[name]
        desc.storage = dtypes.StorageType.GPU_Global
        if name in self._arrays:
            self._arrays[name].status = ArrayStatus.GPU_ONLY
        else:
            array_obj = ArrayType(None, name, ArrayStatus.GPU_ONLY)
            self._arrays[name] = array_obj

    def clone_to_gpu(self, name: str, desc: data.Data) -> str:

        if name in self._arrays and self._arrays[name].status in (ArrayStatus.GPU_ONLY, ArrayStatus.CPU_AND_GPU_THIS_SDFG):
            return self._arrays[name].gpu

        newdesc = desc.clone()
        newdesc.storage = dtypes.StorageType.GPU_Global
        newdesc.transient = True
        new_data_name = self._sdfg.add_datadesc('gpu_' + name, newdesc, find_new_name=True)

        if name in self._arrays:
            self._arrays[name].gpu = new_data_name
            self._arrays[name].status = ArrayStatus.CPU_AND_GPU_THIS_SDFG
        else:
            array_obj = ArrayType(None, new_data_name, ArrayStatus.GPU_ONLY)
            self._arrays[name] = array_obj

        return new_data_name

    def clone_to_cpu(self, name: str, desc: data.Data) -> str:

        if name in self._arrays and self._arrays[name].status in (ArrayStatus.CPU_ONLY, ArrayStatus.CPU_AND_GPU_THIS_SDFG):
            return self._arrays[name].cpu

        new_data_desc = desc.clone()
        new_data_desc.transient = True
        new_data_desc.storage = dtypes.StorageType.CPU_Heap
        new_data_name = 'cpu_' + name

        # Verify that we do not have a name collision
        if new_data_name not in self._sdfg.arrays:
            self._sdfg.add_datadesc(new_data_name, new_data_desc)

        if name in self._arrays:
            self._arrays[name].cpu = new_data_name
            self._arrays[name].status = ArrayStatus.CPU_AND_GPU_THIS_SDFG
        else:
            array_obj = ArrayType(new_data_name, None, ArrayStatus.CPU_ONLY)
            self._arrays[name] = array_obj

        return new_data_name

    def on_gpu(self, name: str) -> bool:
        return name in self._arrays and self._arrays[name].status in (ArrayStatus.GPU_ONLY, ArrayStatus.CPU_AND_GPU_THIS_SDFG)

    def on_cpu(self, name: str) -> bool:
        return name in self._arrays and self._arrays[name].status in (ArrayStatus.CPU_ONLY, ArrayStatus.CPU_AND_GPU_THIS_SDFG)

    def gpu_array(self, name: str) -> str:
        return self._arrays[name].gpu

    def cpu_array(self, name: str) -> str:
        return self._arrays[name].cpu

    def arrays(self) -> Set[str]:
        return set(self._arrays.keys())

    @staticmethod
    def from_sdfg(sdfg: sd.SDFG) -> "SDFGArrays":

        arrays = SDFGArrays(sdfg)

        for n, d in sdfg.arrays.items():
            if d.storage in gpu_storage:

                array_obj = ArrayType(None, n, ArrayStatus.CPU_AND_GPU_OUTER_SDFG)
                arrays._arrays[n] = array_obj

        return arrays



@make_properties
class GPUTransformSDFGCloudSC(transformation.MultiStateTransformation):
    """ Implements the GPUTransformSDFG transformation.

        Transforms a whole SDFG to run on the GPU:
        Steps of the full GPU transform
          0. Acquire metadata about SDFG and arrays
          1. Copy-in state from host to GPU
          2. Recursively schedule all maps for nested SDFGs and states.
          3. Copy-out state from GPU to host
          4. Re-apply simplification to get rid of extra states and
             transients

        What does not work currently:
          - tasklets are not touched
          - inter-state edges are not touched
          - transients are not touched yet
    """

    # FIXME: do we still need it?
    toplevel_trans = Property(desc="Make all GPU transients top-level", dtype=bool, default=True)

    register_trans = Property(desc="Make all transients inside GPU maps registers", dtype=bool, default=True)

    simplify = Property(desc='Reapply simplification after modifying graph', dtype=bool, default=True)

    exclude_copyin = Property(desc="Exclude these arrays from being copied into the device "
                              "(comma-separated)",
                              dtype=str,
                              default='')

    exclude_copyout = Property(desc="Exclude these arrays from being copied out of the device "
                               "(comma-separated)",
                               dtype=str,
                               default='')

    @staticmethod
    def annotates_memlets():
        # Skip memlet propagation for now
        return True

    @classmethod
    def expressions(cls):
        # Matches anything
        return [sd.SDFG('_')]

    def can_be_applied(self, graph, expr_index, sdfg, permissive=False):
        for node, _ in sdfg.all_nodes_recursive():
            # Consume scopes are currently unsupported
            if isinstance(node, (nodes.ConsumeEntry, nodes.ConsumeExit)):
                return False

        for state in sdfg.nodes():
            schildren = state.scope_children()
            for node in schildren[None]:
                # If two top-level tasklets are connected with a code->code
                # memlet, they will transform into an invalid SDFG
                if (isinstance(node, nodes.CodeNode)
                        and any(isinstance(e.dst, nodes.CodeNode) for e in state.out_edges(node))):
                    return False
        return True

    def apply(self, _, sdfg: sd.SDFG):

        #######################################################
        # Step 0: SDFG metadata

        # Find all input and output data descriptors
        input_nodes = []
        output_nodes = []
        global_code_nodes: Dict[sd.SDFGState, nodes.Tasklet] = defaultdict(list)

        arrays = SDFGArrays.from_sdfg(sdfg)

        for state in sdfg.nodes():
            sdict = state.scope_dict()
            for node in state.nodes():
                if (isinstance(node, nodes.AccessNode) and node.desc(sdfg).transient == False):
                    if (state.out_degree(node) > 0 and node.data not in input_nodes):
                        # Special case: nodes that lead to top-level dynamic
                        # map ranges must stay on host
                        for e in state.out_edges(node):
                            last_edge = state.memlet_path(e)[-1]
                            if (isinstance(last_edge.dst, nodes.EntryNode) and last_edge.dst_conn
                                    and not last_edge.dst_conn.startswith('IN_') and sdict[last_edge.dst] is None):
                                break
                        else:
                            input_nodes.append((node.data, node.desc(sdfg)))
                    if (state.in_degree(node) > 0 and node.data not in output_nodes):
                        output_nodes.append((node.data, node.desc(sdfg)))

            # Input nodes may also be nodes with WCR memlets and no identity
            for e in state.edges():
                if e.data.wcr is not None:
                    if (e.data.data not in input_nodes and sdfg.arrays[e.data.data].transient == False):
                        input_nodes.append((e.data.data, sdfg.arrays[e.data.data]))

        start_state = sdfg.start_state
        end_states = sdfg.sink_nodes()

        #######################################################
        # Step 1: Create cloned GPU arrays and replace originals

        for inodename, inode in set(input_nodes):
            if isinstance(inode, data.Scalar):  # Scalars can remain on host
                continue
            if inode.storage == dtypes.StorageType.GPU_Global:
                continue
            arrays.clone_to_gpu(inodename, inode)

        for onodename, onode in set(output_nodes):
            if arrays.on_gpu(onodename):
                continue
            if onode.storage == dtypes.StorageType.GPU_Global:
                continue
            arrays.clone_to_gpu(onodename, onode)

        # Replace nodes
        for state in sdfg.nodes():
            for node in state.nodes():
                if (isinstance(node, nodes.AccessNode) and arrays.on_gpu(node.data)):
                    node.data = arrays.gpu_array(node.data)

        # Replace memlets
        for state in sdfg.nodes():
            for edge in state.edges():
                if arrays.on_gpu(edge.data.data):
                    edge.data.data = arrays.gpu_array(edge.data.data)

        #######################################################
        # Step 2: Create copy-in state
        excluded_copyin = self.exclude_copyin.split(',')

        copyin_state = sdfg.add_state(sdfg.label + '_copyin')
        sdfg.add_edge(copyin_state, start_state, sd.InterstateEdge())

        for nname, desc in dtypes.deduplicate(input_nodes):
            if nname in excluded_copyin or not arrays.on_gpu(nname):
                continue

            src_array = nodes.AccessNode(nname, debuginfo=desc.debuginfo)
            dst_array = nodes.AccessNode(arrays.gpu_array(nname), debuginfo=desc.debuginfo)
            copyin_state.add_node(src_array)
            copyin_state.add_node(dst_array)
            copyin_state.add_nedge(src_array, dst_array, memlet.Memlet.from_array(src_array.data, src_array.desc(sdfg)))

        #######################################################
        # Step 3: Create copy-out state
        excluded_copyout = self.exclude_copyout.split(',')

        copyout_state = sdfg.add_state(sdfg.label + '_copyout')
        for state in end_states:
            sdfg.add_edge(state, copyout_state, sd.InterstateEdge())

        for nname, desc in dtypes.deduplicate(output_nodes):
            if nname in excluded_copyout or not arrays.on_gpu(nname):
                continue
            src_array = nodes.AccessNode(arrays.gpu_array(nname), debuginfo=desc.debuginfo)
            dst_array = nodes.AccessNode(nname, debuginfo=desc.debuginfo)
            copyout_state.add_node(src_array)
            copyout_state.add_node(dst_array)
            copyout_state.add_nedge(src_array, dst_array, memlet.Memlet.from_array(dst_array.data,
                                                                                   dst_array.desc(sdfg)))

        #######################################################
        # Step 4: Change all top-level maps and library nodes to GPU schedule

        gpu_nodes = set()
        for state in sdfg.nodes():
            sdict = state.scope_dict()
            for node in state.nodes():
                if sdict[node] is None:
                    if isinstance(node, (nodes.LibraryNode, nodes.NestedSDFG)):
                        node.schedule = dtypes.ScheduleType.GPU_Default
                        gpu_nodes.add((state, node))
                    elif isinstance(node, nodes.EntryNode):
                        node.schedule = dtypes.ScheduleType.GPU_Device
                        gpu_nodes.add((state, node))

        # NOTE: The outputs of LibraryNodes, NestedSDFGs and Map that have GPU schedule must be moved to GPU memory.
        # TODO: Also use GPU-shared and GPU-register memory when appropriate.
        for state, node in gpu_nodes:
            if isinstance(node, (nodes.LibraryNode, nodes.NestedSDFG)):
                for e in state.out_edges(node):
                    dst = state.memlet_path(e)[-1].dst
                    if isinstance(dst, nodes.AccessNode):
                        arrays.move_to_gpu(dst.data)
            if isinstance(node, nodes.EntryNode):
                for e in state.out_edges(state.exit_node(node)):
                    dst = state.memlet_path(e)[-1].dst
                    if isinstance(dst, nodes.AccessNode):
                        arrays.move_to_gpu(dst.data)

        gpu_scalars = {}
        nsdfgs = []
        changed = True
        # Iterates over Tasklets that not inside a GPU kernel. Such Tasklets must be moved inside a GPU kernel only
        # if they write to GPU memory. The check takes into account the fact that GPU kernels can read host-based
        # Scalars, but cannot write to them.
        while changed:
            changed = False
            for state in sdfg.states():
                for node in state.nodes():
                    # Handle NestedSDFGs later.
                    if isinstance(node, nodes.NestedSDFG):
                        if state.entry_node(node) is None and not scope.is_devicelevel_gpu_kernel(
                                state.parent, state, node):
                            nsdfgs.append((node, state))
                    elif isinstance(node, nodes.Tasklet):
                        if node in global_code_nodes[state]:
                            continue
                        if state.entry_node(node) is None and not scope.is_devicelevel_gpu_kernel(
                                state.parent, state, node):
                            scalars, scalar_output = recursive_out_check(node, state, gpu_scalars)
                            sset, ssout = recursive_in_check(node, state, gpu_scalars)
                            scalars = scalars.union(sset)
                            scalar_output = scalar_output and ssout
                            csdfg = state.parent
                            # If the tasklet is not adjacent only to scalars or it is in a GPU scope.
                            # The latter includes NestedSDFGs that have a GPU-Device schedule but are not in a GPU kernel.
                            if (not scalar_output
                                    or (csdfg.parent is not None
                                        and csdfg.parent_nsdfg_node.schedule == dtypes.ScheduleType.GPU_Default)):
                                global_code_nodes[state].append(node)
                                gpu_scalars.update({k: None for k in scalars})
                                changed = True

        # Apply GPUTransformSDFG recursively to NestedSDFGs.
        for node, state in nsdfgs:
            excl_copyin = set()
            for e in state.in_edges(node):
                src = state.memlet_path(e)[0].src
                if isinstance(src, nodes.AccessNode) and sdfg.arrays[src.data].storage in gpu_storage:
                    excl_copyin.add(e.dst_conn)
                    node.sdfg.arrays[e.dst_conn].storage = sdfg.arrays[src.data].storage
            excl_copyout = set()
            for e in state.out_edges(node):
                dst = state.memlet_path(e)[-1].dst
                if isinstance(dst, nodes.AccessNode) and sdfg.arrays[dst.data].storage in gpu_storage:
                    excl_copyout.add(e.src_conn)
                    node.sdfg.arrays[e.src_conn].storage = sdfg.arrays[dst.data].storage
            # TODO: Do we want to copy here the options from the top-level SDFG?
            node.sdfg.apply_transformations(
                GPUTransformSDFGCloudSC, {
                    'exclude_copyin': ','.join([str(n) for n in excl_copyin]),
                    'exclude_copyout': ','.join([str(n) for n in excl_copyout])
                })

        #######################################################
        # Step 6: Modify transient data storage

        # FIXME: we need a heuristic to decide whether to move a transient to GPU or not.

        const_syms = xfh.constant_symbols(sdfg)

        for state in sdfg.nodes():
            sdict = state.scope_dict()
            for node in state.nodes():
                if isinstance(node, nodes.AccessNode) and node.desc(sdfg).transient:
                    nodedesc = node.desc(sdfg)

                    # Special case: nodes that lead to dynamic map ranges must stay on host
                    if any(isinstance(state.memlet_path(e)[-1].dst, nodes.EntryNode) for e in state.out_edges(node)):
                        continue

                    if state.entry_node(node) is None and nodedesc.storage not in gpu_storage and not scope.is_devicelevel_gpu_kernel(state.parent, state, node):

                        # Scalars were already checked.
                        if isinstance(nodedesc, data.Scalar) and not node.data in gpu_scalars:
                            continue

                        # NOTE: the cloned arrays match too but it's the same storage so we don't care
                        arrays.move_to_gpu(node.data)

                        # Try to move allocation/deallocation out of loops
                        dsyms = set(map(str, nodedesc.free_symbols))
                        if (self.toplevel_trans and not isinstance(nodedesc, (data.Stream, data.View))
                                and len(dsyms - const_syms) == 0):
                            nodedesc.lifetime = dtypes.AllocationLifetime.SDFG
                    elif nodedesc.storage not in gpu_storage:
                        # Make internal transients registers
                        if self.register_trans:
                            nodedesc.storage = dtypes.StorageType.Register

        # Ensure that inter-state edges executed on CPU are using CPU arrays instead of GPU ones.
        gpu_cpu_copies_state = self._handle_interstate_edges(sdfg, arrays, gpu_scalars)

        # Convert CPU tasklets to CPU array
        self._handle_cpu_code(sdfg, arrays, gpu_cpu_copies_state)

        sdfg.save('after_copy_removal.sdfg')

        # Step 9: Simplify
        if not self.simplify:
            return

        sdfg.simplify()

    def _handle_interstate_edges(self, sdfg: sd.SDFG, arrays: SDFGArrays, gpu_scalars) -> Dict[str, List[Tuple[nodes.AccessNode, nodes.AccessNode]]]:

        gpu_cpu_copies_state = defaultdict(list)

        # FIXME: if we should avoid a copy when this is truly a symbol - just a single copy?
        cloned_data = arrays.arrays().union(gpu_scalars.keys())

        for state in list(sdfg.nodes()):
            arrays_used = set()
            for e in sdfg.out_edges(state):
                # Used arrays = intersection between symbols and cloned data
                arrays_used.update(set(e.data.free_symbols) & cloned_data)

            # Create a state and copy out used arrays
            if len(arrays_used) == 0:
                continue

            co_state = sdfg.add_state(state.label + '_icopyout')

            # Reconnect outgoing edges to after interim copyout state
            for e in sdfg.out_edges(state):
                sdutil.change_edge_src(sdfg, state, co_state)
            # Add unconditional edge to interim state
            sdfg.add_edge(state, co_state, sd.InterstateEdge())

            # Add copy-out nodes
            for nname in arrays_used:

                # Handle GPU scalars
                if nname in gpu_scalars:
                    hostname = gpu_scalars[nname]
                    if not hostname:
                        desc = sdfg.arrays[nname].clone()
                        desc.storage = dtypes.StorageType.CPU_Heap
                        desc.transient = True
                        hostname = sdfg.add_datadesc('host_' + nname, desc, find_new_name=True)
                        gpu_scalars[nname] = hostname
                    else:
                        desc = sdfg.arrays[hostname]
                    devicename = nname
                else:

                    if not arrays.on_cpu(nname):
                        hostname = arrays.clone_to_cpu(nname, sdfg.arrays[nname])
                    else:
                        hostname = arrays.cpu_array(nname)

                    devicename = arrays.gpu_array(nname)
                    desc = sdfg.arrays[nname]

                src_array = nodes.AccessNode(devicename, debuginfo=desc.debuginfo)
                dst_array = nodes.AccessNode(hostname, debuginfo=desc.debuginfo)
                co_state.add_node(src_array)
                co_state.add_node(dst_array)
                co_state.add_nedge(src_array, dst_array, memlet.Memlet.from_array(dst_array.data, dst_array.desc(sdfg)))
                for e in sdfg.out_edges(co_state):
                    e.data.replace(devicename, hostname, False)

                if co_state.label in gpu_cpu_copies_state:
                    gpu_cpu_copies_state[co_state.label].append((src_array, dst_array))
                else:
                    gpu_cpu_copies_state[co_state.label] = [(src_array, dst_array)]

        return gpu_cpu_copies_state

    def _handle_cpu_code(
            self,
            sdfg: sd.SDFG,
            arrays: SDFGArrays,
            gpu_cpu_copies: Dict[str, List[Tuple[nodes.AccessNode, nodes.AccessNode]]]
        ):

        """
            Removing redundant copies. There are three ways we can insert non-optimal and duplicated copies into the SDFGF.
            (1) Multiple copies for the same access nodes that was present in many connectors.
            (2) Memlet paths between tasklets - we write to a GPU array and later read from it. We want to avoid
            a situation of tasklet -> CPU array -> GPU array -> CPU array -> tasklet.
            (3) Loops that contain CPU->GPU copies inside. We want to operate on CPU arrays only,
            and add a copy-in and copy-out state.
        """

        """
            Loop removal algorithm.
            (1) For each tasklet, check if its state is inside a loop.
            (2) If it is not inside the loop, then we add the copy-in/copy-out.
            (3) If it is inside a loop, we replace the access node but do not add a copy.
            (4) For each loop, we add a copy.
            FIXME: check if there is a GPU map inside the loop.
        """


        #cpu_gpu_copies: Dict[str, List[Tuple[nodes.AccessNode, nodes.AccessNode]]] = defaultdict(list)

        tasklets = [(state, node) for state in sdfg.nodes() for node in state.nodes() if isinstance(node, nodes.Tasklet)]

        loops = Loops.from_sdfg(sdfg)
        cpu_copies_to_insert: Dict[LoopInfo, List[Tuple[str, str]]] = defaultdict(list)
        gpu_copies_to_insert: Dict[LoopInfo, List[Tuple[str, str]]] = defaultdict(list)

        containers_moved_to_cpu = set()
        for state, node in tasklets:

            loop_info = loops.state_inside_loop(state)

            # Ignore tasklets that are already in the GPU kernel
            if state.entry_node(node) is not None or scope.is_devicelevel_gpu_kernel(state.parent, state, node):
                continue

            # Find CPU tasklets that write to the GPU by checking all outgoing edges
            for outgoing_conn in node.out_connectors:

                for outgoing_edge in state.edges_by_connector(node, outgoing_conn):

                    data_desc = outgoing_edge.dst.desc(sdfg)
                    if data_desc.storage in gpu_storage:

                        # Is there already a CPU array for this? If yes, we want to use it.
                        array_name = outgoing_edge.dst.data

                        # None indicates there was a clone before
                        if arrays.on_cpu(array_name):

                            new_data_name = arrays.cpu_array(array_name)
                            cpu_acc_node = nodes.AccessNode(new_data_name)

                        # There's only a transient GPU array, we need to create a CPU array
                        else:

                            # We allocate it locally only for the purpose of touching some data on the CPU
                            new_data_name = arrays.clone_to_cpu(array_name, data_desc)
                            cpu_acc_node = nodes.AccessNode(new_data_name)

                        gpu_acc_node = outgoing_edge.dst

                        if loop_info is None:

                            # create new edge from CPU access node to GPU access node to trigger a copy
                            # We keep the shape of data access to be the same as the original one
                            cpu_gpu_memlet = memlet.Memlet(
                                expr=None, data=cpu_acc_node.data,
                                subset=outgoing_edge.data.subset,
                                other_subset=outgoing_edge.data.subset
                            )
                            cpu_gpu_memlet._src_subset = outgoing_edge.data.subset
                            state.add_nedge(cpu_acc_node, gpu_acc_node, cpu_gpu_memlet)

                            # now, replace the edge such that the CPU tasklet writes to the CPU array
                            subset = outgoing_edge.data.subset
                            state.remove_edge(outgoing_edge)
                            state.add_edge(
                                node, outgoing_conn,
                                cpu_acc_node, None,
                                memlet.Memlet(expr=None, data=new_data_name, subset=subset)
                            )

                        else:
                            gpu_copies_to_insert[loop_info].append((cpu_acc_node.data, gpu_acc_node.data))
                            containers_moved_to_cpu.add(cpu_acc_node.data)
                            outgoing_edge.data.data = cpu_acc_node.data
                            outgoing_edge.dst.data = cpu_acc_node.data

                    elif outgoing_edge.dst.data in containers_moved_to_cpu:
                        outgoing_edge.data.data = outgoing_edge.dst.data

            # Find CPU tasklets that read from the GPU by checking all incoming edges
            for incoming_conn in node.in_connectors:

                for incoming_edge in state.edges_by_connector(node, incoming_conn):

                    data_desc = incoming_edge.src.desc(sdfg)
                    if data_desc.storage in gpu_storage:

                        # Is there already a CPU array for this? If yes, we want to use it.
                        array_name = incoming_edge.src.data
                        # None indicates there was a clone before
                        if arrays.on_cpu(array_name):

                            new_data_name = arrays.cpu_array(array_name)
                            cpu_acc_node = nodes.AccessNode(new_data_name)

                        # There's only a transient GPU array, we need to create a CPU array
                        else:

                            # We allocate it locally only for the purpose of touching some data on the CPU
                            new_data_name = arrays.clone_to_cpu(array_name, data_desc)
                            cpu_acc_node = nodes.AccessNode(new_data_name)

                        gpu_acc_node = incoming_edge.src

                        if loop_info is None:
                            # create new edge from the GPU access node to CPU access node to trigger a copy
                            # We keep the shape of data access to be the same as the original one
                            gpu_cpu_memlet = memlet.Memlet(
                                expr=None,
                                data=gpu_acc_node.data,
                                subset=incoming_edge.data.subset,
                                other_subset=incoming_edge.data.subset
                            )
                            gpu_cpu_memlet._src_subset = incoming_edge.data.subset
                            state.add_nedge(gpu_acc_node, cpu_acc_node, gpu_cpu_memlet)

                            # now, replace the edge such that the CPU tasklet reads from the CPU array
                            state.remove_edge(incoming_edge)
                            state.add_edge(
                                cpu_acc_node,
                                None,
                                node,
                                incoming_conn,
                                memlet.Memlet(
                                    expr=None, data=new_data_name,
                                    subset=incoming_edge.data.subset
                                )
                            )
                        else:
                            cpu_copies_to_insert[loop_info].append((gpu_acc_node.data, cpu_acc_node.data))
                            incoming_edge.data.data = cpu_acc_node.data
                            incoming_edge.src.data = cpu_acc_node.data
                            containers_moved_to_cpu.add(cpu_acc_node.data)

                    elif incoming_edge.src.data in containers_moved_to_cpu:
                        incoming_edge.data.data = incoming_edge.src.data


        for loop, copies in cpu_copies_to_insert.items():

            loop_guard = loop.guard
            copyin_state = sdfg.add_state(loop_guard.label + '_loopcopyin')

            print(f"Add copy-in state for loop {loop.name}")

            # Reconnect incoming edges to after interim copyout state
            for e in list(sdfg.in_edges(loop_guard)):

                if e.src not in loop.body:
                    sdfg.remove_edge(e)
                    if isinstance(e, graph.MultiConnectorEdge):
                        sdfg.add_edge(e.src, e.src_conn, copyin_state, e.dst_conn, e.data)
                    else:
                        sdfg.add_edge(e.src, copyin_state, e.data)

            # Add unconditional edge from interim state to the loop guard
            sdfg.add_edge(copyin_state, loop_guard, sd.InterstateEdge())

            inserted_copies = set()
            for devicenode, hostnode in copies:

                if devicenode in inserted_copies:
                    continue

                # Add the GPU ->  CPU copy
                src_array = nodes.AccessNode(devicenode)
                dst_array = nodes.AccessNode(hostnode)
                copyin_state.add_node(src_array)
                copyin_state.add_node(dst_array)
                copyin_state.add_nedge(
                    src_array, dst_array,
                    memlet.Memlet.from_array(dst_array.data, dst_array.desc(sdfg))
                )

                inserted_copies.add(devicenode)

        for loop, copies in gpu_copies_to_insert.items():

            loop_guard = loop.guard
            print(f"Add copy-out state for loop {loop.name}")

            copyout_state = sdfg.add_state(loop_guard.label + '_loopcopyout')
            # loop guard must have two outgoing edges - one is an exit edge.
            condition_edge = None
            for e in list(sdfg.out_edges(loop_guard)):
                if e.dst not in loop.body:
                    assert condition_edge is None
                    condition_edge = e

            sdfg.remove_edge(e)
            if isinstance(e, graph.MultiConnectorEdge):
                sdfg.add_edge(e.src, e.src_conn, copyout_state, e.dst_conn, e.data)
            else:
                sdfg.add_edge(e.src, copyout_state, e.data)
            sdfg.add_edge(copyout_state, e.dst, sd.InterstateEdge())

            inserted_copies = set()
            for hostnode, devicenode in copies:

                if hostnode in inserted_copies:
                    continue

                # Add the GPU ->  CPU copy
                src_array = nodes.AccessNode(hostnode)
                dst_array = nodes.AccessNode(devicenode)
                copyout_state.add_node(src_array)
                copyout_state.add_node(dst_array)
                copyout_state.add_nedge(
                    src_array, dst_array,
                    memlet.Memlet.from_array(dst_array.data, dst_array.desc(sdfg))
                )

                inserted_copies.add(hostnode)

    def _remove_copies(self, sdfg: sd.SDFG,
                       cpu_gpu_copies: Dict[str, List[Tuple[nodes.AccessNode, nodes.AccessNode]]],
                       gpu_cpu_copies: Dict[str, List[Tuple[nodes.AccessNode, nodes.AccessNode]]]):

        loop_mapping = {}
        loop_nesting = {}
        loops = {}

        loop_states = {}

        from operator import itemgetter
        from dace.transformation.passes.analysis import AccessSets, FindAccessNodes

        pipeline = Pipeline([AccessSets(), FindAccessNodes()])
        results = {}
        access_sets, access_nodes = itemgetter('AccessSets', 'FindAccessNodes')(pipeline.apply_pass(sdfg, results))

        for loop_guard_label, loop in loops.items():

            loop_guard = loop.guard
            copyin_state = sdfg.add_state(loop_guard.label + '_loopcopyin')

            copies = set()

            # Reconnect incoming edges to after interim copyout state
            for e in sdfg.in_edges(loop_guard):
                sdutil.change_edge_dest(sdfg, loop_guard, copyin_state)
            # Add unconditional edge from interim state to the loop guard
            sdfg.add_edge(copyin_state, loop_guard, sd.InterstateEdge())

            # FIXME: get loop states before
            # FIXME: check for access nodes in this loop - GPU maps?
            for loop_state in loop_states[loop_guard_label]:

                if isinstance(loop_state, sd.SDFGState):

                    if loop_state.label not in gpu_cpu_copies:
                        continue

                    for copy in gpu_cpu_copies[loop_state.label]:

                        # FIXME: two directions of copy
                        devicenode = copy[0]
                        hostnode = copy[1]

                        if devicenode in copies:
                            continue

                        copies.add(devicenode)

                        # Add the GPU ->  CPU copy
                        src_array = nodes.AccessNode(devicenode.data)
                        dst_array = nodes.AccessNode(hostnode.data)
                        copyin_state.add_node(src_array)
                        copyin_state.add_node(dst_array)
                        copyin_state.add_nedge(src_array, dst_array,
                                               memlet.Memlet.from_array(dst_array.data, dst_array.desc(sdfg)))

                        # Remove copies


            print(f"Found a loop at {loop_guard_label} with {len(loop.body.elements)} states")
