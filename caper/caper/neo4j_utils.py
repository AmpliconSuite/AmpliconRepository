import os
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from neo4j import GraphDatabase
from .coamp_graph import Graph

import pandas as pd
import json
import time

neo4j_driver = None

def get_driver():
    # Connect to Neo4j instance
    global neo4j_driver
    if neo4j_driver is None:
        # uri = "bolt://localhost:8000"
        uri = "bolt://localhost:7687"
        neo4j_driver = GraphDatabase.driver(uri, auth=("neo4j", os.environ['NEO4J_PASSWORD_SECRET']))
    return neo4j_driver

def fetch_subgraph_helper(driver, name, min_weight, min_samples, oncogenes, all_edges):
    print("From fetch_subgraph: ")
    print(name)
    print(min_weight)
    print(min_samples)
    print(oncogenes)
    print(all_edges)
    print()
    # ------------------------------- not active -------------------------------
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
        # source
        nodes.setdefault(record['n']['label'], 
                         {'data': {'id': record['n']['label'],
                                   'label': record['n']['label'],
                                   'oncogene': record['n']['oncogene'],
                                   'features': record['n']['features'],
                                   'cell_lines': record['n']['cell_lines']}})
        # target
        nodes.setdefault(record['m']['label'], 
                         {'data': {'id': record['m']['label'],
                                   'label': record['m']['label'],
                                   'oncogene': record['m']['oncogene'],
                                   'features': record['m']['features'],
                                   'cell_lines': record['m']['cell_lines']}})
        # edge
        edgelabel = f"{record['n']['label']} -- {record['m']['label']}"
        edges.setdefault(edgelabel, 
                         {'data': {'id': edgelabel,
                                   'label': edgelabel,
                                   'source': record['n']['label'],
                                   'target': record['m']['label'],
                                   'weight': record['r']['weight'],
                                   'leninter': len(record['r']['inter']),
                                   'inter': record['r']['inter'],
                                   'lenunion': len(record['r']['union']),
                                   'union': record['r']['union'],
                                   'interaction': 'interacts with'
                                   }})
        
    # for record in result:
    #     # record_counter += 1
    #     # ----------------------------------------------------------------------
    #     # print(record)
    #     # source
    #     nodes.setdefault(record['n']['name'], 
    #                      {'data': {'id': record['n']['id'],
    #                                'name': record['n']['name'],
    #                                'oncogene': record['n']['oncogene'],
    #                                'samples': record['n']['samples']}})
    #     # target
    #     nodes.setdefault(record['m']['name'], 
    #                      {'data': {'id': record['m']['id'],
    #                                'name': record['m']['name'],
    #                                'oncogene': record['m']['oncogene'],
    #                                'samples': record['m']['samples']}})
    #     # edge
    #     edges.setdefault(record['n']['name'] + ' -- ' + record['m']['name'], 
    #                      {'data': {'source': record['n']['id'],
    #                                'target': record['m']['id'],
    #                                'weight': record['r']['weight'],
    #                                'leninter': record['r']['leninter'],
    #                                'inter': record['r']['inter'],
    #                                'lenunion': record['r']['lenunion'],
    #                                'union': record['r']['union'],
    #                                'name': record['n']['name'] + ' -- ' + record['m']['name'],
    #                                'interaction': 'interacts with'
    #                                }})
    #     # neighbor nodes/edges
    #     if all_edges and record.get('r2') and record.get('o'):
    #         nodes.setdefault(record['o']['name'], 
    #                          {'data': {'id': record['o']['id'],
    #                                    'name': record['o']['name'],
    #                                    'oncogene': record['o']['oncogene'],
    #                                    'samples': record['o']['samples']}})
    #         triple_intersect = [loc for loc in record['r2']['inter'] if loc in set(record['n']['samples'])]
    #         new_edge_weight = len(triple_intersect) / len(set(record['n']['samples']))
    #         # print(list(nodes.values())[0]['data']["name"], list(nodes.values())[0]['data']["samples"])
    #         # print(record['m']['name'], record['o']['name'], record['m']['samples'], record['o']['samples'], record['r2']['weight'], new_edge_weight)

    #         if record['m']['name'] < record['o']['name']:
    #             edges.setdefault(record['m']['name'] + ' -- ' + record['o']['name'], 
    #                             {'data': {'source': record['m']['id'], 
    #                                     'target': record['o']['id'],
    #                                     'weight': new_edge_weight,
    #                                     'leninter': len(triple_intersect),
    #                                     'inter': triple_intersect,
    #                                     'lenunion': len(list(nodes.values())[0]['data']["samples"]),
    #                                     'union': record['r2']['union'],
    #                                     'name': record['m']['name'] + ' -- ' + record['o']['name'],
    #                                     'interaction': 'interacts with'
    #                                     }})
    #         else:
    #             edges.setdefault(record['o']['name'] + ' -- ' + record['m']['name'], 
    #                             {'data': {'source': record['o']['id'], 
    #                                     'target': record['m']['id'],
    #                                     'weight': new_edge_weight,
    #                                     'leninter': len(triple_intersect),
    #                                     'inter': triple_intersect,
    #                                     'lenunion': len(list(nodes.values())[0]['data']["samples"]),
    #                                     'union': record['r2']['union'],
    #                                     'name': record['o']['name'] + ' -- ' + record['m']['name'],
    #                                     'interaction': 'interacts with'
    #                                     }})
        # ----------------------------------------------------------------------
        #print()
        #print("CURRENT:")
        print(nodes)
        print(edges)
        #print()
    record_end = time.process_time() # time
    print("Record parse runtime: ", record_end - record_start, " seconds") # time
    print("Number of records: ", record_counter)
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

# # CREATE ROUTE with csrf_exempt (optional?)
# def fetch_subgraph(request):
#     driver = get_driver()
#     if request.method == "GET":
#         node_id = request.GET.get('name')
#         min_weight = request.GET.get('min_weight')
#         min_samples = request.GET.get('min_samples')
#         oncogenes = request.GET.get('oncogenes', 'false').lower() == 'true'
#         all_edges = request.GET.get('all_edges', 'false').lower() == 'true'

#     # Create a session and run fetch_subgraph_helper
#     with driver.session() as session:
#         nodes, edges = session.execute_read(fetch_subgraph_helper, node_id, min_weight, min_samples, oncogenes, all_edges)

#         # print(f"\nNodes:\n{nodes}\n\nEdges:\n{edges}")
#         print(f"\nNumber of nodes: {len(nodes)}\nNumber of edges: {len(edges)}\n")

#         if nodes:
#             return JsonResponse({
#                 'nodes': nodes,
#                 'edges': edges
#             })
#         else:
#             return JsonResponse({"error": "Node not found"}), 404

# CREATE ROUTE with csrf_exempt (optional?)
def load_graph(dataset=None):
    driver = get_driver()

    # construct graph
    START_TIME = time.process_time()

    graph = Graph(dataset)
    nodes = graph.Nodes()
    edges = graph.Edges()

    CONSTRUCT_TIME = time.process_time()

    # drop previous graph
    with driver.session() as session:
        session.run("MATCH (n) DETACH DELETE n")
    # import new graph
    with driver.session() as session:
        # add nodes
        session.run("""
            UNWIND $nodes AS row
            CREATE (n:Node {label: row.label, oncogene: row.oncogene, features: row.samples})
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
            MERGE (a)-[:COAMP {weight: toFloat(row.weight), inter: row.inter, union: row.union}]->(b)
            """, edges=edges
        )
    IMPORT_TIME = time.process_time()

    print(f'Construct graph: {CONSTRUCT_TIME-START_TIME} s')
    print(f'Import to neo4j: {IMPORT_TIME-CONSTRUCT_TIME} s')

    return JsonResponse({"message": "Graph loaded successfully"}), 200


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
