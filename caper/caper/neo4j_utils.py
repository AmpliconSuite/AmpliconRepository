import os
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from neo4j import GraphDatabase
from .coamp_graph import Graph

import pandas as pd
import datetime
import json
import time
import hashlib

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


def generate_cache_key(project_ids):
    """
    Generate a unique cache key from a list of project IDs.
    Uses sorted concatenated string, or hash if too long.
    
    Parameters:
        project_ids (list): List of project IDs (can be strings or ObjectIds)
    
    Returns:
        str: Cache key for the project combination
    """
    # Convert all IDs to strings and sort them
    sorted_ids = sorted([str(pid) for pid in project_ids])
    concatenated = "_".join(sorted_ids)
    
    # If the concatenated string is too long (>100 chars), use hash
    if len(concatenated) > 100:
        # Use SHA256 hash for consistent, collision-resistant key
        hash_obj = hashlib.sha256(concatenated.encode('utf-8'))
        return hash_obj.hexdigest()
    
    return concatenated


def check_cached_graph(project_ids):
    """
    Check if a graph for the given project IDs already exists in Neo4j.
    
    Parameters:
        project_ids (list): List of project IDs
    
    Returns:
        bool: True if cached graph exists, False otherwise
    """
    cache_key = generate_cache_key(project_ids)
    driver = get_driver()
    
    with driver.session() as session:
        result = session.run("""
            MATCH (m:GraphMetadata {cache_key: $cache_key})
            RETURN m.cache_key as cache_key, m.timestamp as timestamp, m.node_count as node_count
            """, cache_key=cache_key
        )
        record = result.single()
        
        if record:
            print(f"Found cached graph for cache_key: {cache_key}")
            print(f"  Timestamp: {record['timestamp']}")
            print(f"  Node count: {record['node_count']}")
            return True
        else:
            print(f"No cached graph found for cache_key: {cache_key}")
            return False


def fetch_subgraph_helper(driver, name, min_weight, min_samples, oncogenes, all_edges, cache_key=None):
    # Build cache_key filter for queries
    cache_filter = " AND n.cache_key = $cache_key AND m.cache_key = $cache_key" if cache_key else ""
    cache_filter_o = " AND o.cache_key = $cache_key" if cache_key else ""
    
    if all_edges:
        if oncogenes:
            query = """
            MATCH (n)-[r WHERE r.weight >= {mw} and r.lenunion >= {ms}]-(m WHERE m.oncogene = "True"{cf})
            WHERE n.name = $name{cf_base}
            OPTIONAL MATCH (m)-[r2 WHERE r2.weight >= {mw} and r2.lenunion >= {ms}]-(o WHERE o.oncogene = "True"{cf_o})
            MATCH (o WHERE o.oncogene = "True"{cf_o})-[r3 WHERE r3.weight >= {mw} and r3.lenunion >= {ms}]-(n)
            RETURN n, r, m, r2, o
            """.format(mw = min_weight, ms = min_samples, cf=cache_filter.replace(" AND n.cache_key = $cache_key AND m.cache_key = $cache_key", " AND m.cache_key = $cache_key"), cf_base=" AND n.cache_key = $cache_key" if cache_key else "", cf_o=cache_filter_o)
        else:
            query = """
            MATCH (n)-[r WHERE r.weight >= {mw} and r.lenunion >= {ms}]-(m{cf_simple})
            WHERE n.name = $name{cf_base}
            OPTIONAL MATCH (m)-[r2 WHERE r2.weight >= {mw} and r2.lenunion >= {ms}]-(o{cf_simple})
            MATCH (o{cf_simple})-[r3 WHERE r3.weight >= {mw} and r3.lenunion >= {ms}]-(n)
            RETURN n, r, m, r2, o
            """.format(mw = min_weight, ms = min_samples, cf_simple=" {cache_key: $cache_key}" if cache_key else "", cf_base=" AND n.cache_key = $cache_key" if cache_key else "")
    # --------------------------------------------------------------------------
    else:
        if oncogenes:
            query = """
            MATCH (n)-[r WHERE r.weight >= {mw} and SIZE(r.union) >= {ms}]-(m WHERE m.oncogene = "True"{cf_m})
            WHERE n.label = $name{cf_base}
            RETURN n, r, m
            """.format(mw = min_weight, ms = min_samples, cf_m=" AND m.cache_key = $cache_key" if cache_key else "", cf_base=" AND n.cache_key = $cache_key" if cache_key else "")
            
            prev_query = """
            MATCH (n)-[r WHERE r.weight >= {mw} and r.lenunion >= {ms}]-(m WHERE m.oncogene = "True")
            WHERE n.name = $name
            RETURN n, r, m
            """.format(mw = min_weight, ms = min_samples)
        else:
            query = """
            MATCH (n{cf_simple})-[r WHERE r.weight >= {mw} and SIZE(r.union) >= {ms}]-(m{cf_simple})
            WHERE n.label = $name
            RETURN n, r, m
            """.format(mw = min_weight, ms = min_samples, cf_simple=" {cache_key: $cache_key}" if cache_key else "")

            prev_query = """
            MATCH (n)-[r WHERE r.weight >= {mw} and r.lenunion >= {ms}]-(m)
            WHERE n.name = $name
            RETURN n, r, m
            """.format(mw = min_weight, ms = min_samples)
    # print(query)
    query_start = time.process_time() # time
    result = driver.run(query, name=name, cache_key=cache_key) if cache_key else driver.run(query, name=name)
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

def fetch_subgraph(gene_name, min_weight, min_samples, oncogenes, all_edges, cache_key=None):
    driver = get_driver()
    # Create a session and run fetch_subgraph_helper
    with driver.session() as session:
        nodes, edges = session.execute_read(fetch_subgraph_helper, 
                                            gene_name, 
                                            min_weight, 
                                            min_samples, 
                                            oncogenes, 
                                            all_edges,
                                            cache_key)
    return nodes, edges


# How many ranked genes the landing chart may draw. Charting more than this is
# not readable, and the true totals are reported separately.
OVERVIEW_LIMIT = 300


def fetch_overview_helper(driver, cache_key, limit):
    """Genes ranked by the number of samples carrying them on ecDNA.

    Two lists rather than one: an oncogene-only view sliced off the top of a
    single all-genes list would miss oncogenes sitting below the cut, and the
    caller has to know how many oncogenes exist before it can pick a list.
    """
    scope = " {cache_key: $cache_key}" if cache_key else ""

    ranked = """
    MATCH (n:Node{scope}){onco}
    RETURN n.label AS label, n.oncogene AS oncogene,
           size(coalesce(n.samples, [])) AS amps
    ORDER BY amps DESC, label ASC
    LIMIT $limit
    """

    totals = """
    MATCH (n:Node{scope})
    RETURN count(n) AS gene_total,
           sum(CASE WHEN n.oncogene = "True" THEN 1 ELSE 0 END) AS oncogene_total
    """.format(scope=scope)

    def rows(onco_only):
        query = ranked.format(
            scope=scope,
            onco='\n    WHERE n.oncogene = "True"' if onco_only else ''
        )
        result = driver.run(query, cache_key=cache_key, limit=limit)
        return [
            {
                'label': record['label'],
                'oncogene': record['oncogene'] == 'True',
                'amps': record['amps'],
            }
            for record in result
        ]

    counts = driver.run(totals, cache_key=cache_key).single()

    return {
        'genes': rows(False),
        'oncogenes': rows(True),
        'gene_total': counts['gene_total'] if counts else 0,
        'oncogene_total': counts['oncogene_total'] if counts else 0,
    }


def fetch_overview(cache_key=None, limit=OVERVIEW_LIMIT):
    driver = get_driver()
    with driver.session() as session:
        return session.execute_read(fetch_overview_helper, cache_key, limit)


# CREATE ROUTE with csrf_exempt (optional?)
def load_graph(dataset=None, project_ids=None, force_reload=False):
    """
    Load a graph into Neo4j from a dataset. If project_ids are provided,
    checks for a cached version first.
    
    Parameters:
        dataset: DataFrame containing the project data
        project_ids: List of project IDs used to generate this dataset (for caching)
        force_reload: If True, bypass cache and force regeneration
    
    Returns:
        Graph object
    """
    driver = get_driver()
    
    # Check if we can use cached graph
    if project_ids and not force_reload:
        cache_key = generate_cache_key(project_ids)
        if check_cached_graph(project_ids):
            print(f"Using cached graph for cache_key: {cache_key}")
            # Graph already exists in Neo4j, just construct and return Graph object
            # We still need to return a Graph object for CSV export functionality
            START_TIME = time.process_time()
            graph = Graph(dataset)
            CONSTRUCT_TIME = time.process_time()
            print(f'Construct graph object (cached Neo4j data): {CONSTRUCT_TIME-START_TIME} s')
            return graph

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

    # Generate cache_key for multi-graph support
    cache_key = generate_cache_key(project_ids) if project_ids else None

    # drop previous graph for this cache_key only (allows multiple concurrent caches)
    with driver.session() as session:
        if cache_key:
            # Delete only the graph with this specific cache_key.
            # These must be two independent statements: chaining them with WITH
            # would skip the node cleanup whenever no GraphMetadata node matched,
            # leaving orphaned nodes for the CREATE below to duplicate on top of.
            session.run("""
                MATCH (m:GraphMetadata {cache_key: $cache_key})
                DELETE m
                """, cache_key=cache_key
            )
            session.run("""
                MATCH (n:Node {cache_key: $cache_key})
                DETACH DELETE n
                """, cache_key=cache_key
            )
            print(f"Cleared existing cache for: {cache_key}")
        else:
            # Fallback: no cache_key means delete everything (old behavior)
            session.run("MATCH (n) DETACH DELETE n")
            print("WARNING: No cache_key provided - cleared all graphs")
    
    # import new graph
    with driver.session() as session:
        # add nodes with cache_key for multi-graph support
        session.run("""
            UNWIND $nodes AS row
            CREATE (n:Node {
                cache_key: $cache_key,
                label: row.label, 
                all_labels: row.all_labels, 
                location: row.location, 
                oncogene: row.oncogene, 
                samples: row.samples
            })
            """, nodes=nodes, cache_key=cache_key
        )
        # add indexes for efficient querying
        session.run("""
            CREATE INDEX IF NOT EXISTS FOR (n:Node) ON (n.label)
            """
        )
        session.run("""
            CREATE INDEX IF NOT EXISTS FOR (n:Node) ON (n.cache_key)
            """
        )
        # add edges - match nodes by both label AND cache_key
        session.run("""
            UNWIND $edges AS row
            MATCH (a:Node {label: row.source, cache_key: $cache_key}), 
                  (b:Node {label: row.target, cache_key: $cache_key})
            MERGE (a)-[:COAMP {
                cache_key: $cache_key,
                weight: toFloat(row.weight), 
                inter: row.inter, 
                union: row.union, 
                distance: toInteger(row.distance), 
                p_values: row.p_values, 
                odds_ratios: row.odds_ratios, 
                q_values: row.q_values
            }]->(b)
            """, edges=edges, cache_key=cache_key
        )
        
        # Add metadata node for caching if project_ids provided
        if project_ids:
            cache_key = generate_cache_key(project_ids)
            session.run("""
                CREATE (m:GraphMetadata {
                    cache_key: $cache_key,
                    project_ids: $project_ids,
                    timestamp: timestamp(),
                    node_count: $node_count,
                    edge_count: $edge_count
                })
                """, 
                cache_key=cache_key,
                project_ids=[str(pid) for pid in project_ids],
                node_count=len(nodes),
                edge_count=len(edges)
            )
            print(f"Stored graph metadata with cache_key: {cache_key}")
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


def _clear_cache_keys(session, cache_keys):
    """
    Delete the metadata node and the graph data for each of the given cache keys.

    Both must go: dropping only the GraphMetadata node leaves the nodes/edges
    orphaned in Neo4j, where they are invisible to the cache listing but still
    matched by every subsequent query against that cache_key.
    """
    for cache_key in cache_keys:
        session.run("""
            MATCH (m:GraphMetadata {cache_key: $cache_key})
            DELETE m
            """, cache_key=cache_key
        )
        session.run("""
            MATCH (n:Node {cache_key: $cache_key})
            DETACH DELETE n
            """, cache_key=cache_key
        )
        print(f"Cleared graph cache for cache_key: {cache_key}")


def clear_graph_cache(project_ids=None):
    """
    Clear cached graph(s) from Neo4j, including the graph data itself.

    Parameters:
        project_ids: List of project IDs identifying one cache entry, or None to
                     clear every cache entry

    Returns:
        int: Number of cache entries cleared
    """
    driver = get_driver()

    with driver.session() as session:
        if project_ids:
            cache_keys = [generate_cache_key(project_ids)]
        else:
            # Every cache_key present in the DB, including orphans that have graph
            # data but lost their GraphMetadata node to an earlier partial clear.
            result = session.run("""
                MATCH (m:GraphMetadata)
                RETURN collect(DISTINCT m.cache_key) AS keys
                """
            )
            cache_keys = set(result.single()['keys'] or [])
            result = session.run("""
                MATCH (n:Node)
                WHERE n.cache_key IS NOT NULL
                RETURN collect(DISTINCT n.cache_key) AS keys
                """
            )
            cache_keys.update(result.single()['keys'] or [])
            cache_keys = sorted(cache_keys)

        _clear_cache_keys(session, cache_keys)
        print(f"Cleared {len(cache_keys)} graph cache entry/entries")
        return len(cache_keys)


def clear_graph_cache_for_project(project_id):
    """
    Clear every cached graph that was built from a project, whichever combination
    of projects it was selected alongside.

    Call this whenever a project's underlying data changes or the project goes
    away (edit into a new version, sample add, delete, version rollback) so the
    cache does not keep serving — or merely keep storing — a graph built from
    data that no longer exists.

    Parameters:
        project_id: A single project ID (linkid / ObjectId / str)

    Returns:
        int: Number of cache entries cleared
    """
    driver = get_driver()
    project_id = str(project_id)

    with driver.session() as session:
        # project_ids is stored on the metadata node as a list of strings.
        result = session.run("""
            MATCH (m:GraphMetadata)
            WHERE $project_id IN m.project_ids
            RETURN collect(DISTINCT m.cache_key) AS keys
            """, project_id=project_id
        )
        cache_keys = result.single()['keys'] or []

        # A single-project graph is keyed by the bare project ID (see
        # generate_cache_key), so it can be cleaned up even if its metadata node
        # is missing.
        if project_id not in cache_keys:
            cache_keys.append(project_id)

        _clear_cache_keys(session, cache_keys)
        print(f"Cleared {len(cache_keys)} graph cache entry/entries for project {project_id}")
        return len(cache_keys)


# Rough per-entity storage overhead in Neo4j, on top of the variable-length string
# payload measured below. Nodes carry label/all_labels/location/oncogene/samples/
# cache_key; COAMP relationships carry weight/distance/cache_key plus three
# four-element float arrays. Only ever used to give admins a sense of scale.
_NODE_OVERHEAD_BYTES = 150
_EDGE_OVERHEAD_BYTES = 200


def measure_cached_graphs(session):
    """
    Measure what is actually stored in Neo4j, grouped by cache_key.

    This counts the real nodes and relationships rather than trusting the counts
    recorded on the GraphMetadata node, so it also picks up cache keys whose
    metadata is gone but whose graph data is still taking up space.

    Returns:
        dict: cache_key -> {node_count, edge_count, size_bytes}
    """
    stats = {}

    node_result = session.run("""
        MATCH (n:Node)
        WHERE n.cache_key IS NOT NULL
        RETURN n.cache_key AS cache_key,
               count(n) AS node_count,
               sum(
                 reduce(t = 0, s IN coalesce(n.samples, []) | t + size(s)) +
                 reduce(t = 0, s IN coalesce(n.all_labels, []) | t + size(s)) +
                 reduce(t = 0, s IN coalesce(n.location, []) | t + size(s)) +
                 size(coalesce(n.label, ''))
               ) AS payload_bytes
        """
    )
    for record in node_result:
        entry = stats.setdefault(record['cache_key'],
                                 {'node_count': 0, 'edge_count': 0, 'size_bytes': 0})
        entry['node_count'] = record['node_count']
        entry['size_bytes'] += (record['payload_bytes'] or 0) + \
            record['node_count'] * _NODE_OVERHEAD_BYTES

    edge_result = session.run("""
        MATCH ()-[r:COAMP]->()
        WHERE r.cache_key IS NOT NULL
        RETURN r.cache_key AS cache_key,
               count(r) AS edge_count,
               sum(
                 reduce(t = 0, s IN coalesce(r.inter, []) | t + size(s)) +
                 reduce(t = 0, s IN coalesce(r.union, []) | t + size(s))
               ) AS payload_bytes
        """
    )
    for record in edge_result:
        entry = stats.setdefault(record['cache_key'],
                                 {'node_count': 0, 'edge_count': 0, 'size_bytes': 0})
        entry['edge_count'] = record['edge_count']
        entry['size_bytes'] += (record['payload_bytes'] or 0) + \
            record['edge_count'] * _EDGE_OVERHEAD_BYTES

    return stats


def _timestamp_to_datetime(ms):
    """Convert a Cypher timestamp() value (epoch milliseconds) to a datetime."""
    if ms is None:
        return None
    try:
        return datetime.datetime.fromtimestamp(ms / 1000.0)
    except (TypeError, ValueError, OSError):
        return None


def list_cached_graphs():
    """
    List all cached graphs in Neo4j with their metadata and measured size.

    Entries with graph data but no GraphMetadata node are included and flagged as
    orphaned - they are unreachable as a cache hit but still occupy Neo4j.

    Returns:
        list: List of dictionaries containing cache information
    """
    driver = get_driver()

    with driver.session() as session:
        measured = measure_cached_graphs(session)

        result = session.run("""
            MATCH (m:GraphMetadata)
            RETURN m.cache_key as cache_key,
                   m.project_ids as project_ids,
                   m.timestamp as timestamp,
                   m.node_count as node_count,
                   m.edge_count as edge_count
            ORDER BY m.timestamp DESC
            """
        )

        cached_graphs = []
        for record in result:
            stats = measured.pop(record['cache_key'],
                                 {'node_count': 0, 'edge_count': 0, 'size_bytes': 0})
            cached_graphs.append({
                'cache_key': record['cache_key'],
                'project_ids': record['project_ids'],
                # Cypher timestamp() is epoch milliseconds; templates need a datetime
                'timestamp': _timestamp_to_datetime(record['timestamp']),
                # Prefer the measured counts; the recorded ones are what the graph
                # was built with, which drifts if anything went wrong since.
                'node_count': stats['node_count'],
                'edge_count': stats['edge_count'],
                'recorded_node_count': record['node_count'],
                'recorded_edge_count': record['edge_count'],
                'size_bytes': stats['size_bytes'],
                'orphaned': False,
            })

        # Whatever is left in `measured` has graph data but no metadata node.
        for cache_key, stats in measured.items():
            cached_graphs.append({
                'cache_key': cache_key,
                'project_ids': [],
                'timestamp': None,
                'node_count': stats['node_count'],
                'edge_count': stats['edge_count'],
                'recorded_node_count': None,
                'recorded_edge_count': None,
                'size_bytes': stats['size_bytes'],
                'orphaned': True,
            })

        return cached_graphs


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
