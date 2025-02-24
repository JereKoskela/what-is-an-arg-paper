"""
Utilities for generating and converting ARGs in various formats.
"""
import random
import math
import collections
import dataclasses
from typing import List
from typing import Any

import tskit
import numpy as np

NODE_IS_RECOMB = 1 << 1
NODE_IS_NONCOAL_CA = 1 << 2  # TODO better name


def draw_arg(ts):

    node_labels = {}
    for node in ts.nodes():
        label = str(node.id)
        if node.flags == NODE_IS_RECOMB:
            label = f"R{node.id}"
        elif node.flags == NODE_IS_NONCOAL_CA:
            label = f"N{node.id}"
        node_labels[node.id] = label
    print(ts.draw_text(node_labels=node_labels))


# AncestryInterval is the equivalent of msprime's Segment class. The
# important different here is that we don't associated nodes with
# individual intervals here: because this is an ARG, nodes that
# we pass through are recorded.
#
# (The ancestral_to field is also different here, but that's because
# I realised that the way we're tracking extant ancestral material
# in msprime is unnecessarily complicated, and we can actually
# track it locally. There is potentially quite a large performance
# increase available in msprime from this.)


@dataclasses.dataclass
class AncestryInterval:
    """
    Records that the specified interval contains genetic material ancestral
    to the specified number of samples.
    """

    left: int
    right: int
    ancestral_to: int


@dataclasses.dataclass
class Lineage:
    """
    A single lineage that is present during the simulation of the coalescent
    with recombination. The node field represents the last (as we go backwards
    in time) genome in which an ARG event occured. That is, we can imagine
    a lineage representing the passage of the ancestral material through
    a sequence of ancestral genomes in which it is not modified.
    """

    node: int
    ancestry: List[AncestryInterval]

    def __str__(self):
        s = f"{self.node}:["
        for interval in self.ancestry:
            s += str((interval.left, interval.right, interval.ancestral_to))
            s += ", "
        if len(self.ancestry) > 0:
            s = s[:-2]
        return s + "]"

    @property
    def num_recombination_links(self):
        """
        The number of positions along this lineage's genome at which a recombination
        event can occur.
        """
        return self.right - self.left - 1

    @property
    def left(self):
        """
        Returns the leftmost position of ancestral material.
        """
        return self.ancestry[0].left

    @property
    def right(self):
        """
        Returns the rightmost position of ancestral material.
        """
        return self.ancestry[-1].right

    def split(self, breakpoint):
        """
        Splits the ancestral material for this lineage at the specified
        breakpoint, and returns a second lineage with the ancestral
        material to the right.
        """
        left_ancestry = []
        right_ancestry = []
        for interval in self.ancestry:
            if interval.right <= breakpoint:
                left_ancestry.append(interval)
            elif interval.left >= breakpoint:
                right_ancestry.append(interval)
            else:
                assert interval.left < breakpoint < interval.right
                left_ancestry.append(dataclasses.replace(interval, right=breakpoint))
                right_ancestry.append(dataclasses.replace(interval, left=breakpoint))
        self.ancestry = left_ancestry
        return Lineage(self.node, right_ancestry)


# The details of the machinery in the next two functions aren't important.
# It could be done more cleanly and efficiently. The basic idea is that
# we're providing a simple way to find the overlaps in the ancestral
# material of two or more lineages, abstracting the complex interval
# logic out of the main simulation.
@dataclasses.dataclass
class MappingSegment:
    left: int
    right: int
    value: Any = None


def overlapping_segments(segments):
    """
    Returns an iterator over the (left, right, X) tuples describing the
    distinct overlapping segments in the specified set.
    """
    S = sorted(segments, key=lambda x: x.left)
    n = len(S)
    # Insert a sentinel at the end for convenience.
    S.append(MappingSegment(math.inf, 0))
    right = S[0].left
    X = []
    j = 0
    while j < n:
        # Remove any elements of X with right <= left
        left = right
        X = [x for x in X if x.right > left]
        if len(X) == 0:
            left = S[j].left
        while j < n and S[j].left == left:
            X.append(S[j])
            j += 1
        j -= 1
        right = min(x.right for x in X)
        right = min(right, S[j + 1].left)
        yield left, right, X
        j += 1

    while len(X) > 0:
        left = right
        X = [x for x in X if x.right > left]
        if len(X) > 0:
            right = min(x.right for x in X)
            yield left, right, X


def merge_ancestry(lineages):
    """
    Return an iterator over the ancestral material for the specified lineages.
    For each distinct interval at which ancestral material exists, we return
    the AncestryInterval and the corresponding list of lineages.
    """
    # See note above on the implementation - this could be done more cleanly.
    segments = []
    for lineage in lineages:
        for interval in lineage.ancestry:
            segments.append(
                MappingSegment(interval.left, interval.right, (lineage, interval))
            )

    for left, right, U in overlapping_segments(segments):
        ancestral_to = sum(u.value[1].ancestral_to for u in U)
        interval = AncestryInterval(left, right, ancestral_to)
        yield interval, [u.value[0] for u in U]


def arg_sim(n, rho, L, seed=None):
    """
    Simulate an ancestry-resolved ARG under the coalescent with recombination
    and return the tskit TreeSequence object.

    NOTE! This hasn't been statistically tested and is probably not correct.
    """
    rng = random.Random(seed)
    tables = tskit.TableCollection(L)
    tables.nodes.metadata_schema = tskit.MetadataSchema.permissive_json()
    lineages = []
    for _ in range(n):
        node = tables.nodes.add_row(time=0, flags=tskit.NODE_IS_SAMPLE)
        lineages.append(Lineage(node, [AncestryInterval(0, L, 1)]))

    t = 0
    while len(lineages) > 0:
        # print(f"t = {t:.2f} k = {len(lineages)}")
        # for lineage in lineages:
        #     print(f"\t{lineage}")
        lineage_links = [lineage.num_recombination_links for lineage in lineages]
        total_links = sum(lineage_links)
        re_rate = total_links * rho
        t_re = math.inf if re_rate == 0 else rng.expovariate(re_rate)
        k = len(lineages)
        ca_rate = k * (k - 1) / 2
        t_ca = rng.expovariate(ca_rate)
        t_inc = min(t_re, t_ca)
        t += t_inc

        if t_inc == t_re:
            left_lineage = rng.choices(lineages, weights=lineage_links)[0]
            breakpoint = rng.randrange(left_lineage.left + 1, left_lineage.right)
            assert left_lineage.left < breakpoint < left_lineage.right
            right_lineage = left_lineage.split(breakpoint)
            child = left_lineage.node
            for lineage in left_lineage, right_lineage:
                lineage.node = tables.nodes.add_row(
                    flags=NODE_IS_RECOMB, time=t, metadata={"breakpoint": breakpoint}
                )
                for interval in lineage.ancestry:
                    tables.edges.add_row(
                        interval.left, interval.right, lineage.node, child
                    )
            lineages.append(right_lineage)
        else:
            a = lineages.pop(rng.randrange(len(lineages)))
            b = lineages.pop(rng.randrange(len(lineages)))
            c = Lineage(len(tables.nodes), [])
            flags = NODE_IS_NONCOAL_CA
            for interval, intersecting_lineages in merge_ancestry([a, b]):
                if len(intersecting_lineages) > 1:
                    flags = 0  # This is a coalescence, treat this as ordinary tree node
                if interval.ancestral_to < n:
                    c.ancestry.append(interval)
                for lineage in intersecting_lineages:
                    tables.edges.add_row(
                        interval.left, interval.right, c.node, lineage.node
                    )
            tables.nodes.add_row(flags=flags, time=t, metadata={})
            if len(c.ancestry) > 0:
                lineages.append(c)

    tables.sort()
    return tables.tree_sequence()


def unresolved_arg_sim(n, rho, L, seed=None):
    """
    Simulate an non-ancestry-resolved ARG under the coalescent with recombination
    and return the tskit TableCollection object. In this only the existance
    of an edge between two different nodes is encoded, and the specific intervals
    of ancestral material not recorded.

    For common ancestor events we have two child nodes a and b, and a parent
    c. We record edges (-inf, inf, a, c) and (-inf, inf, b, c).

    For recombination events we have one child u and two parent nodes v an w,
    and a breakpoint x. We record edges (-inf, x, u, v) and (x, inf, u, w).
    We also record the breakpoint x with the parent nodes v and w, although
    it is strictly redundant.

    The resulting ARG is identical to that simulated using the arg_sim function
    above, and can be converted into an ancestry-resolved ARG for use in
    tskit using the convert_arg function.

    NOTE! This hasn't been statistically tested and is probably not correct.
    """
    rng = random.Random(seed)
    tables = tskit.TableCollection(L)
    tables.nodes.metadata_schema = tskit.MetadataSchema.permissive_json()

    lineages = []
    for _ in range(n):
        node = tables.nodes.add_row(time=0, flags=tskit.NODE_IS_SAMPLE)
        lineages.append(Lineage(node, [AncestryInterval(0, L, 1)]))
    t = 0
    while len(lineages) > 0:
        # print(f"t = {t:.2f} k = {len(lineages)}")
        # for lineage in lineages:
        #     print(f"\t{lineage}")
        lineage_links = [lineage.num_recombination_links for lineage in lineages]
        total_links = sum(lineage_links)
        re_rate = total_links * rho
        t_re = math.inf if re_rate == 0 else rng.expovariate(re_rate)
        k = len(lineages)
        ca_rate = k * (k - 1) / 2
        t_ca = rng.expovariate(ca_rate)
        t_inc = min(t_re, t_ca)
        t += t_inc

        if t_inc == t_re:
            left_lineage = rng.choices(lineages, weights=lineage_links)[0]
            child = left_lineage.node
            breakpoint = rng.randrange(left_lineage.left + 1, left_lineage.right)
            assert left_lineage.left < breakpoint < left_lineage.right
            left_lineage.node = tables.nodes.add_row(
                flags=NODE_IS_RECOMB, time=t, metadata={"breakpoint": breakpoint}
            )
            tables.edges.add_row(-math.inf, breakpoint, left_lineage.node, child)
            right_lineage = left_lineage.split(breakpoint)
            right_lineage.node = tables.nodes.add_row(
                flags=NODE_IS_RECOMB, time=t, metadata={"breakpoint": breakpoint}
            )
            tables.edges.add_row(breakpoint, math.inf, right_lineage.node, child)
            lineages.append(right_lineage)
        else:
            a = lineages.pop(rng.randrange(len(lineages)))
            b = lineages.pop(rng.randrange(len(lineages)))
            c = Lineage(len(tables.nodes), [])
            flags = NODE_IS_NONCOAL_CA
            for interval, intersecting_lineages in merge_ancestry([a, b]):
                if len(intersecting_lineages) > 1:
                    flags = 0  # This is a coalescence, treat this as ordinary tree node
                if interval.ancestral_to < n:
                    c.ancestry.append(interval)
            tables.nodes.add_row(flags=flags, time=t, metadata={})
            tables.edges.add_row(-math.inf, math.inf, c.node, a.node)
            tables.edges.add_row(-math.inf, math.inf, c.node, b.node)
            # print(f"\tc = {c}")
            if len(c.ancestry) > 0:
                lineages.append(c)
    return tables


def convert_arg(tables):
    """
    Converts the specified non-ancestry tracking ARG to a tskit ARG.

    Note: the implementation is currently quite ropey, and has a
    few assumptions about the form of the input that aren't necessary.
    Ideally we'd work just from the edge table and node times, as
    we can work out the existance of recombinations and the breakpoints
    from it.
    """
    out = tables.copy()
    out.edges.clear()
    nodes = sorted(tables.nodes, key=lambda x: x.time)

    children = collections.defaultdict(list)
    for edge in tables.edges:
        children[edge.parent].append(edge.child)

    lineages = []
    node_id = 0
    n = 0
    while node_id < len(nodes) and (nodes[node_id].flags & tskit.NODE_IS_SAMPLE != 0):
        node = nodes[node_id]
        # print("sample:", node)
        assert nodes[node_id].time == 0
        lineages.append(
            Lineage(node_id, [AncestryInterval(0, tables.sequence_length, 1)])
        )
        node_id += 1
        n += 1
    while node_id < len(nodes):
        node = nodes[node_id]
        # print("VISIT", node_id, node.time)
        # for lineage in lineages:
        #     print(f"\t{lineage}")
        if (node.flags & NODE_IS_RECOMB) != 0:
            left_parent = node_id
            node_id += 1
            right_parent = node_id

            # print("RE EVENT", left_parent, right_parent)
            child = children[left_parent][0]
            assert len(children[left_parent]) == 1
            assert len(children[right_parent]) == 1
            assert children[right_parent][0] == child

            # print(f"parent = {parent} child = {child}")
            breakpoint = node.metadata["breakpoint"]
            for left_lineage in lineages:
                if left_lineage.node == child:
                    break
            right_lineage = left_lineage.split(breakpoint)
            left_lineage.node = left_parent
            right_lineage.node = right_parent
            lineages.append(right_lineage)
            for lineage in [left_lineage, right_lineage]:
                for interval in lineage.ancestry:
                    out.edges.add_row(
                        interval.left,
                        interval.right,
                        lineage.node,
                        child,
                    )
        else:
            parent = node_id
            # print("COAL", parent)
            # print(children[parent])
            assert len(children[parent]) == 2
            children_lineages = []
            for child in children[parent]:
                for j in range(len(lineages)):
                    if lineages[j].node == child:
                        children_lineages.append(lineages.pop(j))
                        break
            a, b = children_lineages
            c = Lineage(parent, [])
            flags = NODE_IS_NONCOAL_CA
            for interval, intersecting_lineages in merge_ancestry([a, b]):
                if len(intersecting_lineages) > 1:
                    flags = 0  # This is a coalescence, treat this as ordinary tree node
                if interval.ancestral_to < n:
                    c.ancestry.append(interval)
                for lineage in intersecting_lineages:
                    out.edges.add_row(
                        interval.left, interval.right, c.node, lineage.node
                    )
            assert node.flags == flags
            # print(f"\ta = {a}")
            # print(f"\tb = {b}")
            # print(f"\tc = {c}")
            if len(c.ancestry) > 0:
                lineages.append(c)
        # print(f"t = {node.time:.2f}")
        # for lineage in lineages:
        #     print(f"\t{lineage}")
        node_id += 1
    # print(out)
    out.sort()
    return out.tree_sequence()


n = 5
rho = 0.3
L = 10
for seed in range(1, 100):
    ts = arg_sim(n, rho, L, seed=seed)
    tables = unresolved_arg_sim(n, rho, L, seed=seed)
    # print(ts.tables)
    ts2 = convert_arg(tables)

    draw_arg(ts)
    # draw_arg(ts2)

    ts.tables.assert_equals(ts2.tables)
