import os
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from neo4j import GraphDatabase
from .coamp_graph import Graph

import pandas as pd
import json
import time

neo4j_driver = None

import logging
logging.getLogger("neo4j").setLevel(logging.WARNING)

def get_driver():
    # Connect to Neo4j instance
    global neo4j_driver
    if neo4j_driver is None:
        # uri = "bolt://localhost:8000"
        uri = "bolt://localhost:7687"
        neo4j_driver = GraphDatabase.driver(uri, auth=("neo4j", os.environ['NEO4J_PASSWORD_SECRET']))
    return neo4j_driver

def fetch_subgraph_helper(driver, name, min_weight, min_samples, oncogenes, all_edges):
    if all_edges:
        if oncogenes:
            query = """
            MATCH (n)-[r WHERE r.weight >= {mw} and r.lenunion >= {ms}]-(m WHERE m.oncogene = "True")
            WHERE n.name = $name
            OPTIONAL MATCH (m)-[r2 WHERE r2.weight >= {mw} and r2.lenunion >= {ms}]-(o WHERE o.oncogene = "True")
            MATCH (o WHERE o.oncogene = "True")-[r3 WHERE r3.weight >= {mw} and r3.lenunion >= {ms}]-(n)
            RETURN n, r, m, r2, o
            """.format(mw = min_weight, ms = min_samples)
        else:
            query = """
            MATCH (n)-[r WHERE r.weight >= {mw} and r.lenunion >= {ms}]-(m)
            WHERE n.name = $name
            OPTIONAL MATCH (m)-[r2 WHERE r2.weight >= {mw} and r2.lenunion >= {ms}]-(o)
            MATCH (o)-[r3 WHERE r3.weight >= {mw} and r3.lenunion >= {ms}]-(n)
            RETURN n, r, m, r2, o
            """.format(mw = min_weight, ms = min_samples)
    # --------------------------------------------------------------------------
    else:
        if oncogenes:
            query = """
            MATCH (n)-[r WHERE r.weight >= {mw} and SIZE(r.union) >= {ms}]-(m WHERE m.oncogene = "True")
            WHERE n.label = $name
            RETURN n, r, m
            """.format(mw = min_weight, ms = min_samples)
            
            prev_query = """
            MATCH (n)-[r WHERE r.weight >= {mw} and r.lenunion >= {ms}]-(m WHERE m.oncogene = "True")
            WHERE n.name = $name
            RETURN n, r, m
            """.format(mw = min_weight, ms = min_samples)
        else:
            query = """
            MATCH (n)-[r WHERE r.weight >= {mw} and SIZE(r.union) >= {ms}]-(m)
            WHERE n.label = $name
            RETURN n, r, m
            """.format(mw = min_weight, ms = min_samples)

            prev_query = """
            MATCH (n)-[r WHERE r.weight >= {mw} and r.lenunion >= {ms}]-(m)
            WHERE n.name = $name
            RETURN n, r, m
            """.format(mw = min_weight, ms = min_samples)
    # print(query)
    query_start = time.process_time() # time
    result = driver.run(query, name=name)
    query_end = time.process_time() # time
    print("Query runtime: ", query_end - query_start, " seconds") # time
    
    nodes = {}
    edges = {}
    # print("DISPLAY RECORDS")
    # print()
    record_start = time.process_time() # time
    record_counter = 0

    for record in result:
        record_counter += 1

        # Always add both nodes (setdefault won't overwrite if already exists)
        source_label = record['n']['label']
        target_label = record['m']['label']

        # source
        if source_label not in nodes:
            nodes[source_label] = {
                'data': {
                    'id': source_label,
                    'label': source_label,
                    'all_labels': record['n'].get('all_labels', []),
                    'location': record['n'].get('location', []),
                    'oncogene': record['n'].get('oncogene', 'False'),
                    'samples': record['n'].get('samples', [])
                }
            }

        # target
        if target_label not in nodes:
            nodes[target_label] = {
                'data': {
                    'id': target_label,
                    'label': target_label,
                    'all_labels': record['m'].get('all_labels', []),
                    'location': record['m'].get('location', []),
                    'oncogene': record['m'].get('oncogene', 'False'),
                    'samples': record['m'].get('samples', [])
                }
            }

        # edge
        edgelabel = f"{source_label} -- {target_label}"

        # Safely extract p_values, odds_ratios, q_values as lists
        p_values = record['r'].get('p_values', [-1, -1, -1, -1])
        odds_ratios = record['r'].get('odds_ratios', [-1, -1, -1, -1])
        q_values = record['r'].get('q_values', [-1, -1, -1, -1])

        edges.setdefault(edgelabel,
                         {'data': {'id': edgelabel,
                                   'label': edgelabel,
                                   'source': record['n']['label'],
                                   'target': record['m']['label'],
                                   'weight': record['r'].get('weight', 0),
                                   'leninter': len(record['r'].get('inter', [])),
                                   'inter': record['r'].get('inter', []),
                                   'lenunion': len(record['r'].get('union', [])),
                                   'union': record['r'].get('union', []),
                                   'distance': record['r'].get('distance', -1),
                                   'pval_single_interval': p_values[0],
                                   'qval_single_interval': q_values[0],
                                   'odds_ratio_single_interval': odds_ratios[0],
                                   'pval_multi_interval': p_values[1],
                                   'qval_multi_interval': q_values[1],
                                   'odds_ratio_multi_interval': odds_ratios[1],
                                   'pval_multi_chromosomal': p_values[2],
                                   'qval_multi_chromosomal': q_values[2],
                                   'odds_ratio_multi_chromosomal': odds_ratios[2],
                                   'interaction': 'interacts with'
                                   }})

    record_end = time.process_time() # time
    print("Record parse runtime: ", record_end - record_start, " seconds") # time
    print("Number of records: ", record_counter)
    print(f"Unique nodes returned: {len(nodes)}")
    print(f"Unique edges returned: {len(edges)}")
    node_ids = [n['data']['id'] for n in nodes.values()]

    for edge_key, edge in list(edges.items())[:5]:  # First 5 edges
        print(f"Edge: {edge['data']['source']} -> {edge['data']['target']}")
        if edge['data']['source'] not in node_ids:
            print(f"  WARNING: Source {edge['data']['source']} not in nodes!")
        if edge['data']['target'] not in node_ids:
            print(f"  WARNING: Target {edge['data']['target']} not in nodes!")

    return list(nodes.values()), list(edges.values())

def fetch_subgraph(gene_name, min_weight, min_samples, oncogenes, all_edges):
    driver = get_driver()
    # Create a session and run fetch_subgraph_helper
    with driver.session() as session:
        nodes, edges = session.execute_read(fetch_subgraph_helper, 
                                            gene_name, 
                                            min_weight, 
                                            min_samples, 
                                            oncogenes, 
                                            all_edges)
    return nodes, edges

# CREATE ROUTE with csrf_exempt (optional?)
def load_graph(dataset=None):
    driver = get_driver()

    # construct graph
    START_TIME = time.process_time()

    graph = Graph(dataset)
    nodes = graph.Nodes()
    edges = graph.Edges()

    print(f"Graph constructed: {len(nodes)} nodes, {len(edges)} edges")

    if not nodes:
        print("ERROR: No nodes created!")
        return JsonResponse({"error": "Graph construction failed - no nodes created"}), 400

    # reformat for neo4j
    for node in nodes:
        del node['features']
        del node['intervals']
        for k, v in node.items():
            if isinstance(v, set):
                node[k] = list(v)
        if 'location' in node:
            node['location'] = [str(i) for i in node['location']]

    for edge in edges:
        del edge['p_d_D']
        for k, v in edge.items():
            if isinstance(v, set):
                edge[k] = list(v)

    CONSTRUCT_TIME = time.process_time()

    # drop previous graph
    with driver.session() as session:
        session.run("MATCH (n) DETACH DELETE n")
    # import new graph
    with driver.session() as session:
        # add nodes
        session.run("""
            UNWIND $nodes AS row
            CREATE (n:Node {label: row.label, all_labels: row.all_labels, location: row.location, oncogene: row.oncogene, samples: row.samples})
            """, nodes=nodes
        )
        # add index on label (can be done once)
        session.run("""
            CREATE INDEX IF NOT EXISTS FOR (n:Node) ON (n.label)
            """
        )
        # add edges
        session.run("""
            UNWIND $edges AS row
            MATCH (a:Node {label: row.source}), (b:Node {label: row.target})
            MERGE (a)-[:COAMP {weight: toFloat(row.weight), inter: row.inter, union: row.union, distance: toInteger(row.distance), p_values: row.p_values, odds_ratios: row.odds_ratios, q_values: row.q_values}]->(b)
            """, edges=edges
        )
        # session.run("""
        #     UNWIND $edges AS row
        #     MATCH (a:Node {label: row.source}), (b:Node {label: row.target})
        #     MERGE (a)-[:COAMP {odds_ratio_multi_chromosomal: toFloat(row.odds_ratio_multi_chromosomal), pval_multi_chromosomal: toFloat(row.pval_multi_chromosomal), qval_multi_chromosomal: toFloat(row.qval_multi_chromosomal), odds_ratio_multi_interval: toFloat(row.odds_ratio_multi_interval), pval_multi_interval: toFloat(row.pval_multi_interval), qval_multi_interval: toFloat(row.qval_multi_interval), odds_ratio_single_interval: toFloat(row.odds_ratio_single_interval), distance: toInteger(row.distance), pval_single_interval: toFloat(row.pval_single_interval), qval_single_interval: toFloat(row.qval_single_interval), weight: toFloat(row.weight), inter: row.inter, union: row.union}]->(b)
        #     """, edges=edges
        # )
    IMPORT_TIME = time.process_time()

    print(f'Construct graph: {CONSTRUCT_TIME-START_TIME} s')
    print(f'Import to neo4j: {IMPORT_TIME-CONSTRUCT_TIME} s')

    # Return the graph object so it can be cached for CSV export
    return graph


def test_fetch_subgraph():
    driver = get_driver()
    test_node_name = "CASC15"
    # Create a session and run fetch_subgraph
    with driver.session() as session:
        nodes, edges = session.execute_read(fetch_subgraph, test_node_name, 0.1, 1, False, False)
        # Prepare the output dictionary
        output = {
            'nodes': nodes,
            'edges': edges
        }
        # Write the output to a file
        with open(f'{test_node_name}_output.json', 'w') as outfile:
            json.dump(output, outfile, indent=4)
