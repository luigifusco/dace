# Copyright 2019-2021 ETH Zurich and the DaCe authors. All rights reserved.
import unittest
from dace.sdfg.graph import *


class TestOrderedGraphs(unittest.TestCase):
    def test_ordered_digraph(self):
        g = OrderedDiGraph()
        g.add_edge(0, 7, "abc")
        g.add_edge(0, 3, "def")
        g.add_edge(0, 5, "ghi")
        g.add_edge(9, 0, "jkl")
        nodes = list(g.nodes())
        edges = list(g.edges())
        self.assertEqual(g.number_of_nodes(), 5)
        self.assertEqual(g.number_of_edges(), 4)
        self.assertEqual(g.in_degree(0), 1)
        self.assertEqual(g.out_degree(0), 3)
        self.assertEqual(nodes[0], 0)
        self.assertEqual(nodes[1], 7)
        self.assertEqual(nodes[2], 3)
        self.assertEqual(nodes[3], 5)
        self.assertEqual(nodes[4], 9)
        self.assertEqual(edges[0].data, "abc")
        self.assertEqual(edges[1].data, "def")
        self.assertEqual(edges[2].data, "ghi")
        self.assertEqual(edges[3].data, "jkl")
        g.remove_edge(Edge(0, 3, "def"))
        g.remove_edge(Edge(9, 0, "jkl"))
        nodes = list(g.nodes())
        edges = list(g.edges())
        self.assertEqual(g.number_of_nodes(), 5)
        self.assertEqual(g.number_of_edges(), 2)
        self.assertEqual(g.in_degree(0), 0)
        self.assertEqual(g.out_degree(0), 2)
        self.assertEqual(nodes[0], 0)
        self.assertEqual(nodes[1], 7)
        self.assertEqual(nodes[2], 3)
        self.assertEqual(nodes[3], 5)
        self.assertEqual(nodes[4], 9)
        self.assertEqual(len(edges), 2)
        self.assertEqual(edges[0].data, "abc")
        self.assertEqual(edges[1].data, "ghi")
        g.remove_node(7)
        self.assertEqual(g.number_of_nodes(), 4)
        self.assertEqual(g.number_of_edges(), 1)
        self.assertEqual(len(edges[0]), 3)

    def test_ordered_multidigraph(self):

        g = OrderedMultiDiGraph()
        e0 = g.add_edge(0, 3, "abc")
        e1 = g.add_edge(0, 3, "def")
        e2 = g.add_edge(0, 3, "ghi")
        e3 = g.add_edge(0, 3, "jkl")
        g.add_edge(0, 4, "mno")
        g.add_edge(4, 3, "pqr")
        self.assertEqual(g.number_of_nodes(), 3)
        self.assertEqual(g.number_of_edges(), 6)
        self.assertEqual(g.in_degree(0), 0)
        self.assertEqual(g.in_degree(3), 5)
        self.assertEqual(g.out_degree(0), 5)
        nodes = list(g.nodes())
        edges = list(g.edges())
        self.assertEqual(nodes[0], 0)
        self.assertEqual(nodes[1], 3)
        self.assertEqual(nodes[2], 4)
        self.assertEqual(edges[0], e0)
        self.assertEqual(edges[1], e1)
        self.assertEqual(edges[2], e2)
        self.assertEqual(edges[3], e3)
        g.remove_edge(e2)
        self.assertEqual(g.number_of_nodes(), 3)
        self.assertEqual(g.number_of_edges(), 5)
        self.assertEqual(g.in_degree(0), 0)
        self.assertEqual(g.in_degree(3), 4)
        self.assertEqual(g.out_degree(0), 4)
        edges = list(g.edges())
        self.assertEqual(edges[0], e0)
        self.assertEqual(edges[1], e1)
        self.assertEqual(edges[2], e3)
        g.remove_node(4)
        self.assertEqual(g.number_of_nodes(), 2)
        self.assertEqual(g.number_of_edges(), 3)
        self.assertEqual(g.in_degree(3), 3)
        self.assertEqual(g.out_degree(0), 3)
        self.assertEqual(len(edges[0]), 3)
        h = OrderedMultiDiGraph()
        e0 = h.add_edge(0, 1, None)
        e1 = h.add_edge(0, 2, None)
        e2 = h.add_edge(1, 3, None)
        e3 = h.add_edge(3, 4, None)
        e4 = h.add_edge(1, 5, None)
        e5 = h.add_edge(2, 6, None)
        e6 = h.add_edge(6, 7, None)
        e7 = h.add_edge(6, 8, None)
        e8 = h.add_edge(2, 6, None)
        bfs_edges = h.bfs_edges(0)
        self.assertEqual(next(bfs_edges), e0)
        self.assertEqual(next(bfs_edges), e1)
        self.assertEqual(next(bfs_edges), e2)
        self.assertEqual(next(bfs_edges), e4)
        self.assertEqual(next(bfs_edges), e5)
        self.assertEqual(next(bfs_edges), e8)
        self.assertEqual(next(bfs_edges), e3)
        self.assertEqual(next(bfs_edges), e6)
        self.assertEqual(next(bfs_edges), e7)
    
    def test_dfs_edges(self):

        sdfg = dace.SDFG('test_dfs_edges')
        before, _, _, _ = sdfg.add_loop(sdfg.add_state(), sdfg.add_state(), sdfg.add_state(), 'i', '0', 'i < 10',
                                        'i + 1')
        
        visited_edges = list(sdfg.dfs_edges(before))
        assert len(visited_edges) == len(set(visited_edges))
        assert all(e in visited_edges for e in sdfg.edges())


if __name__ == "__main__":
    unittest.main()
