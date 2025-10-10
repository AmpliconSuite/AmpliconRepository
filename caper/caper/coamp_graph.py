import re
import pandas as pd
import numpy as np
import networkx as nx
from collections import defaultdict
import json
import time
import os
from intervaltree import IntervalTree
from scipy.stats import expon
from scipy.stats import gamma
from scipy.stats import chi2
from statsmodels.stats.multitest import fdrcorrection


class Graph:
    def __init__(self, dataset=None, focal_amp="ecDNA", by_sample=False, merge_cutoff=50000,
                 pdD_model='gamma_random_breakage', construct_graph=True):
        """
        Parameters:
            self (Graph) : Graph object
            dataset (tsv file) : AA aggregated results file
            focal_amp (str) : type of focal amplification (ecDNA, BFB, etc.)
            by_sample (bool) : define co-amplifications by sample or by feature (default)
        Return:
            None
        """
        if dataset is None:
            print("Error: provide Amplicon Architect annotations")
        else:
            # processing parameters
            self.MERGE_CUTOFF = merge_cutoff
            self.pdD_MODEL = pdD_model

            # graph properties
            self.nodes = []
            self.edges = []
            self.gene_records = {}
            self.name_to_record = {}
            self.name_to_edge = {}

            start_time = time.time()
            # define reference genomes to process
            ref_genomes = ['hg38_gtf', 'hg19_gtf']
            base_ref = 'GRCh38'

            ref_files = [self.get_gene_bed_path(ref) for ref in ref_genomes]
            base_ref_file = self.get_gene_bed_path(base_ref)

            # load gene location data for base reference
            locs_by_base_ref = self.import_locs(base_ref_file)
            print(
                f"Retrieved locations for {len(locs_by_base_ref)} {base_ref} genes in {time.time() - start_time:.2f} seconds")

            # build comprehensive group of genes containing location data
            start_record = time.time()
            self.gene_records, self.name_to_record = self.create_gene_records(ref_files, locs_by_base_ref)
            print(f"Loaded {len(self.gene_records)} genes from provided reference files")
            print(f"Gene record creation time: {time.time() - start_record:.2f} seconds")

            # test
            print(len(self.name_to_record.keys()))

            loc_count = sum([len(v['location']) > 0 for v in self.gene_records.values()])
            print(f"Matched location data for {loc_count} genes")

            # preprocess dataset
            preprocessed_dataset = self.preprocess_dataset(dataset, focal_amp)
            self.total_samples = len(preprocessed_dataset)

            self.preprocessed_dataset = preprocessed_dataset

            # create nodes and edges from the combined dataset
            if construct_graph:
                self.create_nodes(preprocessed_dataset)
                self.create_edges(by_sample)

    def get_gene_bed_path(self, reference_genome):
        """
        Get the appropriate gene annotation bed file path based on reference genome

        Parameters:
            reference_genome (str) : Reference genome version
        Return:
            str : Path to the appropriate gene annotation bed file
        """
        caper_root = os.environ.get('CAPER_ROOT', '')
        bed_dir = os.path.join(caper_root, 'caper', 'bed_files')

        # Map reference genome to appropriate bed file
        genome_to_bed = {
            'hg38_gtf': 'Genes_hg38.gff',
            'hg19_gtf': 'Genes_July_2010_hg19.gff',
            'GRCh38': 'hg38_genes.bed',
            'hg38': 'hg38_genes.bed',
            'GRCh38_viral': 'hg38_genes.bed',
            'GRCh37': 'GRCh37_genes.bed',
            'hg19': 'hg19_genes.bed',
            # 'mm10': 'mm10_genes.bed'
        }

        # Get bed file name, default to hg38 if not found
        bed_file = genome_to_bed.get(reference_genome, 'hg38_genes.bed')

        # Check if file exists, warn if not
        bed_path = os.path.join(bed_dir, bed_file)
        if not os.path.exists(bed_path):
            print(f"Warning: Bed file {bed_path} not found. Gene coordinates may not be available.")

        return bed_path

    def import_locs(self, bed_file):
        """
        Return a dict of format: {gene (ncbi id): ( start chromosome (string),
                                                    start coordinate (int),
                                                    end coordinate (int) )}
        """
        locs = {}
        try:
            gene_coords = pd.read_csv(bed_file, sep="\t", header=None)

            # Normalize chromosome names to always have 'chr' prefix
            def normalize_chr(chrom):
                chrom = str(chrom)
                if not chrom.startswith('chr'):
                    return 'chr' + chrom
                return chrom

            gene_coords['chr_normalized'] = gene_coords[0].apply(normalize_chr)

            # Convert to dictionary
            locs = dict(zip(gene_coords[3],
                            zip(gene_coords['chr_normalized'],
                                gene_coords[1].astype(int),
                                gene_coords[2].astype(int))))
        except Exception as e:
            print(f"Error importing gene coordinates from {bed_file}: {e}")

        return locs

    def create_gene_records(self, ref_files, locs_by_base_ref):
        gene_records = {}
        # parse name and id data
        names_ids = pd.DataFrame(columns=['ID', 'Name'])
        for ref in ref_files:
            df = pd.read_csv(ref, sep="\t", comment="#", header=None)
            df = df.assign(Name=lambda x: (x[8].str.extract(r'Name=([^;]+)')))
            df = df.assign(ID=lambda x: (x[8].str.extract(r'Accession=([^;]+)')))
            df.dropna(inplace=True)
            names_ids = pd.concat([names_ids, df[['ID', 'Name']]])

        # map names to ids
        name_to_ids = names_ids.groupby('Name')['ID'].agg(set).to_dict()

        # construct name-id graph
        G = nx.Graph()
        for name, ids in name_to_ids.items():
            for id in ids:
                G.add_edge(name, id)

        base_ref_names = set(locs_by_base_ref.keys())
        # find connected components
        for component in nx.connected_components(G):
            names_in_component = {node for node in component if node in name_to_ids}
            first_name = list(names_in_component)[0]
            # overlay location info from base ref if possible
            location = ()
            base_names = names_in_component & base_ref_names
            if len(base_names) > 0:
                first_name = list(base_names)[0]
                location = locs_by_base_ref[first_name]
            # store the group, arbitrarily labeling by first name in component
            gene_records[first_name] = {'label': first_name,
                                        'all_labels': names_in_component,
                                        'oncogene': '',
                                        'features': set(),
                                        'samples': set(),
                                        'location': location,
                                        'intervals': defaultdict(lambda: defaultdict(int))
                                        }
        # create retrieval index
        name_to_record = {}
        for __, record in gene_records.items():
            for name in record['all_labels']:
                name_to_record[name] = record

        return gene_records, name_to_record

    def merge_intervals(self, location):
        """
        Parameters:
            location (str): 'Location' column of one feature in input dataset
            self.MERGE_CUTOFF (int): merge intervals < cutoff distance (bp)
        Return:
            merged_intervals (list): list of intervals as lists of chr,start,end
        """
        merged_intervals = []

        # extract all chr:start-end patterns
        matches = re.findall(r"'?([A-Za-z0-9_]+):(\d+)-(\d+)'?", location)
        matches = [[chrom, int(start), int(end)] for chrom, start, end in matches]

        if not matches:
            return []

        curr_chr, curr_start, curr_end = matches[0]

        for chrom, start, end in matches[1:]:
            same_chr = (chrom == curr_chr)
            close_or_overlap = (start - curr_end) <= self.MERGE_CUTOFF

            if same_chr and close_or_overlap:
                # merge by extending end coordinate
                curr_end = max(curr_end, end)
            else:
                # push current and start new
                merged_intervals.append([curr_chr, curr_start, curr_end])
                curr_chr, curr_start, curr_end = chrom, start, end

        # add the last interval
        merged_intervals.append([curr_chr, curr_start, curr_end])
        return merged_intervals

    def extract_genes(self, input):
        """
        Parameters:
            input (str) : ['A', 'B', 'C'] or ["'A'", "'B'", "'C'"]
        Return:
            list: ["A", "B", "C"]
        """
        pattern = r"['\"]?([\w./-]+)['\"]?"
        genelist = re.findall(pattern, input)
        return genelist

    def preprocess_dataset(self, dataset, focal_amp):
        """
        streamline preprocessing of intervals and dataset reformatting before graph construction
        """
        # subset dataset by focal amplification type
        filter_start = time.time()
        filtered_dataset = dataset[dataset['Classification'] == focal_amp].copy()
        filter_end = time.time()
        print(
            f"Filtering features took {filter_end - filter_start:.4f} seconds, resulting in {len(filtered_dataset)} features")

        # Normalize chromosome names in Location column to always have 'chr' prefix
        def normalize_location(location_str):
            """Add 'chr' prefix to any chromosome identifiers that lack it"""
            location_str = str(location_str)

            # Match chromosome:position patterns and add chr if missing
            def add_chr(match):
                full = match.group(0)
                chrom = match.group(1)
                rest = match.group(2)
                if chrom.startswith('chr'):
                    return full  # already has chr prefix
                return f"chr{chrom}{rest}"

            location_str = re.sub(r"'?([A-Za-z0-9_]+)(:\d+-\d+)'?", add_chr, location_str)
            return location_str

        filtered_dataset['Location'] = filtered_dataset['Location'].apply(normalize_location)

        # reformat columns
        reformat_start = time.time()
        filtered_dataset['Merged_Intervals'] = filtered_dataset.apply(
            lambda row: self.merge_intervals(str(row['Location'])), axis=1
        )
        filtered_dataset['Oncogenes'] = filtered_dataset.apply(
            lambda row: set(self.extract_genes(str(row['Oncogenes']))), axis=1
        )
        filtered_dataset['All_genes'] = filtered_dataset.apply(
            lambda row: self.extract_genes(str(row['All_genes'])), axis=1
        )
        reformat_end = time.time()
        print(f"Preprocessing intervals and reformatting dataset took {reformat_end - reformat_start:.4f} seconds")

        # # NOTE sample vs. feature level distinction made in CreateEdges()
        # # if specified, group by sample name for sample-level co-amplifications
        # if self.by_sample:
        # 	group_start = time.time()
        # 	filtered_dataset = filtered_dataset.groupby('Sample_name', as_index=False).agg({
        # 		'Feature_ID': lambda col: [item for item in col],
        # 		'Oncogenes': lambda col: set([x for item in col for x in item]),
        # 		'All_genes': lambda col: list(set([x for item in col for x in item])),
        # 		'Merged_Intervals': lambda col: [x for item in col for x in item]
        # 	})
        # 	group_end = time.time()
        # 	print(f"Grouping features by sample took {group_end - group_start:.4f} seconds, resulting in {len(filtered_dataset)} samples")

        # return relevant columns
        preprocessed_dataset = filtered_dataset[['Sample_name',
                                                 'Feature_ID',
                                                 'Reference_version',
                                                 'Merged_Intervals',
                                                 'Oncogenes',
                                                 'All_genes']].copy()
        return preprocessed_dataset

    def create_nodes(self, dataset):
        """
        Create nodes while tracking the reference genome for each gene
        """
        start_time = time.time()
        print(f"Starting CreateNodes with {len(dataset)} rows")

        process_start = time.time()
        gene_count = 0
        feature_id_counter = 0
        interval_id_counter = 0

        genes_with_loc_counter = 0
        genes_with_interval_counter = 0
        genes_with_chr_no_interval_counter = 0
        genes_with_no_chr_match_counter = 0
        self.genes_with_no_chr_match_list = []

        # for each feature or sample
        for ind, row in dataset.iterrows():
            # get the properties in this feature
            # if ind < 10:
            #     print(row['Sample_name'], row['Feature_ID'], row['Reference_version'], row['Merged_Intervals'], row['Oncogenes'], row['All_genes'])

            oncogenes = row.get('Oncogenes')
            all_genes = row.get('All_genes')
            feature = row.get('Feature_ID')
            sample = row.get('Sample_name')
            intervals = row.get('Merged_Intervals')

            # build per-chromosome interval trees
            interval_lookup = defaultdict(IntervalTree)
            for interval in intervals:
                ichr, istart, iend = interval
                tree = interval_lookup[ichr]
                tree.addi(istart, iend, interval_id_counter)
                interval_id_counter += 1

            # # store interval coordinates of feature
            # interval_lookup = defaultdict(list)
            # for interval in intervals:
            # 	ichr, istart, iend = interval
            # 	interval_lookup[ichr].append([istart, iend, interval_id_counter])
            # 	interval_id_counter += 1

            gene_count += len(all_genes)

            # update the record for each gene in this feature
            for gene in all_genes:
                if gene in self.name_to_record:
                    record = self.name_to_record[gene]
                    record['oncogene'] = str(gene in oncogenes)
                    record['features'].add(feature)
                    record['samples'].add(sample)
                    record['updated'] = True
                    # get the id of the interval the gene is amplified on
                    record['intervals'][sample][feature_id_counter] = None
                    if record['location']:
                        genes_with_loc_counter += 1
                        chrom, start, end = record['location']
                        if chrom in interval_lookup:
                            hits = interval_lookup[chrom][start:end]  # fast lookup
                            if hits:
                                # pick one (if multiple intervals overlap)
                                hit = next(iter(hits))
                                genes_with_interval_counter += 1
                                record['intervals'][sample][feature_id_counter] = hit.data
                            else:
                                # interval_found = False
                                # for istart, iend, id in interval_lookup[chr]:
                                # 	if (start > istart and start < iend):
                                # 		genes_with_interval_counter += 1
                                # 		record['intervals'][sample][feature_id_counter] = id
                                # 		interval_found = True
                                # 		break
                                # if not interval_found:
                                genes_with_chr_no_interval_counter += 1
                                record['genes_with_chr_no_interval_counter'] = True
                        else:
                            genes_with_no_chr_match_counter += 1
                            record['genes_with_no_chr_match_counter'] = True
                            self.genes_with_no_chr_match_list.append((chrom, interval_lookup.keys()))

                else:
                    print(f"Warning: {gene} does not match to any gene in the provided reference files")
            feature_id_counter += 1

        # store nodes that were updated in gene record
        self.nodes = [r for r in self.gene_records.values() if r.get('updated')]

        check_feature_sample_count = 0
        for node in self.nodes:
            if len(node['features']) > len(node['samples']):
                check_feature_sample_count += 1

        print(f"Note: {check_feature_sample_count} genes are amplified on multiple feature IDs in the same sample")

        test_size_1 = len([r for r in self.gene_records.values() if r.get('genes_with_chr_no_interval_counter')])
        test_size_2 = len([r for r in self.gene_records.values() if r.get('genes_with_no_chr_match_counter')])
        print(f"TEST: {genes_with_loc_counter} searches where gene's location is found")
        print(
            f"TEST: {genes_with_interval_counter} searches where gene's chr on interval and gene's location is matched to an interval")
        print(
            f"TEST: {genes_with_chr_no_interval_counter} searches where gene's chr on interval but gene's location is NOT matched to an interval ({test_size_1} unique nodes)")
        print(
            f"TEST: {genes_with_no_chr_match_counter} searches where gene's chr not on interval ({test_size_2} unique nodes)")

        process_end = time.time()
        print(
            f"Processing {gene_count} genes took {process_end - process_start:.4f} seconds, resulting in {len(self.nodes)} unique nodes")

        no_interval_count = sum([len(r['intervals']) == 0 for r in self.nodes])
        print(f"{no_interval_count} genes were marked amplified on a set of empty intervals")

        total_time = time.time() - start_time
        print(f"Total CreateNodes execution: {total_time:.4f} seconds")

    def create_edges(self, by_sample):
        start_time = time.time()
        print(f"Starting CreateEdges with {len(self.nodes)} nodes")

        # group by samples or features
        grouping = 'samples' if by_sample else 'features'

        # build reverse index to map grouping to nodes
        index_start = time.time()
        grouping_to_nodes = defaultdict(list)
        for record in self.nodes:
            for group in record[grouping]:
                grouping_to_nodes[group].append(record['label'])
        print(
            f"Building {grouping} index took {time.time() - index_start:.4f} seconds: {len(grouping_to_nodes)} unique {grouping}")

        # collect potential node pairs
        pairs_start = time.time()
        potential_pairs = set()
        for nodelist in grouping_to_nodes.values():
            for i, node1 in enumerate(nodelist):
                for node2 in nodelist[i + 1:]:
                    pair = tuple(sorted((node1, node2)))  # avoid duplicates
                    potential_pairs.add(pair)
        print(
            f"Collecting potential pairs took {time.time() - pairs_start:.4f} seconds: {len(potential_pairs)} unique pairs")

        # early return if no pairs
        if not potential_pairs:
            self.edges = []
            print("No potential pairs found, returning early")
            return

        # construct edges from pairs
        construct_start = time.time()
        for pair in potential_pairs:
            record_a = self.name_to_record[pair[0]]
            record_b = self.name_to_record[pair[1]]
            inter = record_a['samples'] & record_b['samples']
            # add edge if a shared sample is found
            if inter:
                union = record_a['samples'] | record_b['samples']
                weight = len(inter) / len(union)
                locs_found = record_a['location'] and record_b['location']
                distance = self.distance(record_a, record_b) if locs_found else -1
                p_d_D = self.p_d_D(distance)
                self.edges.append({'source': record_a['label'],
                                   'target': record_b['label'],
                                   'weight': weight,
                                   'inter': inter,
                                   'union': union,
                                   'distance': distance,
                                   'p_d_D': p_d_D,
                                   'p_values': [-1] * 4,
                                   'odds_ratios': [-1] * 4,
                                   'q_values': [-1] * 4
                                   })
        print(
            f"Constructing edges took {time.time() - construct_start:.4f} seconds: {len(self.edges)} edges with non-empty intersections")

        # create retrieval index for edges
        self.name_to_edge = {
            f"{a}-{b}": edge
            for edge in self.edges
            for a, b in [(edge["source"], edge["target"]),
                         (edge["target"], edge["source"])]
        }

        # perform significance testing on edges
        p_start = time.time()
        na_counter = 0
        for edge in self.edges:
            self.perform_tests(edge)
            if edge['p_values'] == [-1, -1, -1, -1]:
                na_counter += 1
        print(
            f"Performing significance tests took {time.time() - p_start:.4f} seconds")
        print(
            f"P-values were not assigned to {na_counter} edges")

        # calculate q-values for each test
        q_start = time.time()
        p_lists = [[] for _ in range(4)]
        for edge in self.edges:
            for i in range(4):
                p_lists[i].append(edge['p_values'][i])

        q_lists = [self.q_val(p_list) for p_list in p_lists]

        for idx, edge in enumerate(self.edges):
            edge['q_values'] = [q_lists[i][idx] for i in range(4)]
        print(
            f"Calculating q-values took {time.time() - q_start:.4f} seconds")

        total_time = time.time() - start_time
        print(f"Total CreateEdges execution: {total_time:.4f} seconds")

    def distance(self, record_a, record_b):
        """
        expects non-empty location attributes
        """
        distance = -1
        if record_a['location'][0] == record_b['location'][0]:
            (start1, end1), (start2, end2) = sorted([
                (record_a['location'][1], record_a['location'][2]),
                (record_b['location'][1], record_b['location'][2])
            ])
            distance = max(0, start2 - end1)
        return distance

    def p_d_D(self, distance):
        """
        generate p_val based on naive random breakage model, considering distance
        but not number of incidents of co-amplification
        """
        if distance < 0:
            return -1
        models = {'gamma_random_breakage': [0.3987543483729932,
                                            1.999999999999,
                                            1749495.5696758535],
                  'gamma_capped_5m': [0.4528058380301946,
                                      1.999999999999,
                                      1192794.3765480835],
                  'merge25kbp_cap2mbp': [0.5567064701123039,
                                         1.9999999999999996,
                                         534068.5378861207],
                  'merge25kbp_cap1mbp': [0.7420123072378633,
                                         1.9999999999999998,
                                         203145.72909328266]}
        params = models[self.pdD_MODEL]
        cdf = gamma.cdf(distance, a=params[0], loc=params[1], scale=params[2])
        return 1 - cdf

    def perform_tests(self, edge, verbose=False):
        """
        perform all applicable significance tests and fill corresponding properties
        fill lists in order:
            [0] single interval
            [1] multi interval
            [2] multi chromosomal
            [3] multi ecDNA
        """
        record_a = self.name_to_record[edge['source']]
        record_b = self.name_to_record[edge['target']]

        # early return if missing location data
        if not record_a['location'] or not record_b['location']:
            return

        # test
        if (
                record_a.get('genes_with_chr_no_interval_counter')
                or record_a.get('genes_with_no_chr_match_counter')
                or record_b.get('genes_with_chr_no_interval_counter')
                or record_b.get('genes_with_no_chr_match_counter')
        ):
            edge['missing_interval_data'] = True

        samples = list(edge['inter'])
        tested = [False, False, False, False]
        log = []
        log.append(
            f"{len(samples)} shared samples\n"
            f"Missing interval data: {'missing_interval_data' in edge}\n"
        )

        # for each co-amplified sample, determine the type of co-amplification
        # and run the appropriate test if not previously done
        i = 0
        while i < len(samples) and False in tested:
            intervals_a = record_a['intervals'][samples[i]]
            intervals_b = record_b['intervals'][samples[i]]
            features_ab = set(intervals_a.keys()) & set(intervals_b.keys())

            log_test_update = False

            # if len(features_ab) > 1:
            # 	print('Note: unexpected number of shared features in sample')

            same_chr = record_a['location'][0] == record_b['location'][0]
            same_feature = len(features_ab) > 0
            intervals_to_check = [(intervals_a[f], intervals_b[f])
                                  for f in features_ab
                                  if intervals_a[f] is not None and intervals_b[f] is not None]

            if intervals_to_check:
                same_interval = any(a == b for a, b in intervals_to_check)
                diff_interval = any(a != b for a, b in intervals_to_check)
            else:
                # No valid intervals to compare
                same_interval = False
                diff_interval = False

            # [0] single interval test
            if not tested[0] and same_interval:
                p_value, odds_ratio = self.single_interval(edge, record_a, record_b)
                edge['p_values'][0] = p_value
                edge['odds_ratios'][0] = odds_ratio
                tested[0] = True
                log_test_update = True
                log.append(
                    f"[i={i}] Sample '{samples[i]}'\n"
                    f"Ran single_interval test: p={p_value:.3g}"
                )

            # [1] multi interval test
            if not tested[1] and same_chr and same_feature and diff_interval:
                p_value, odds_ratio = self.multi_interval(edge, record_a, record_b)
                edge['p_values'][1] = p_value
                edge['odds_ratios'][1] = odds_ratio
                tested[1] = True
                log_test_update = True
                log.append(
                    f"[i={i}] Sample '{samples[i]}'\n"
                    f"Ran multi_interval test: p={p_value:.3g}"
                )

            # [2] multi chromosomal test
            if not tested[2] and not same_chr:
                p_value, odds_ratio = self.multi_chromosomal(edge, record_a, record_b)
                edge['p_values'][2] = p_value
                edge['odds_ratios'][2] = odds_ratio
                tested[2] = True
                log_test_update = True
                log.append(
                    f"[i={i}] Sample '{samples[i]}'\n"
                    f"Ran multi_chromosomal test: p={p_value:.3g}"
                )

            # [3] multi ecDNA test
            if not tested[3] and not same_feature:
                p_value, odds_ratio = self.multi_ecdna(edge, record_a, record_b)
                edge['p_values'][3] = p_value
                edge['odds_ratios'][3] = odds_ratio
                tested[3] = True
                log.append(
                    f"[i={i}] Sample '{samples[i]}'\n"
                    f"Ran multi_ecDNA test: p={p_value:.3g}"
                )

            if log_test_update:
                log.append(
                    f"{record_a['label']} features: {sorted(intervals_a)}\n"
                    f"{record_b['label']} features: {sorted(intervals_b)}\n"
                    f"Shared features: {sorted(features_ab)}\n"
                )
                log.append(
                    f"same_chr: {same_chr} \nsame_feature: {same_feature} \nsame_interval: {same_interval} \ndiff_interval: {diff_interval}\n\n"
                )

            i += 1

        if verbose:
            print("\n".join(log))

    def chi_squared_helper(self, obs, exp, verbose=False):
        # apply Haldane correction if any observed category count is zero
        if 0 in obs or 0 in exp:
            obs = [o + 0.5 for o in obs]
            exp = [e + 0.5 for e in exp]

        # total_obs = sum(obs)
        # total_exp = sum(exp)
        # obs_freq = [o / total_obs for o in obs]
        # exp_freq = [e / total_exp for e in exp]

        test_statistic = sum([(o - e) * (o - e) / e for o, e in zip(obs, exp)])
        cdf = chi2.cdf(test_statistic, df=1)
        p_val_two_sided = 1 - cdf
        diagonal_residual_sum = ((obs[0] - exp[0]) + (obs[3] - exp[3]))

        odds_ratio = min(obs[0] / exp[0], 1e9)  # cap ultra large to prevent infinite odds ratios

        # convert to one-sided p-value
        if diagonal_residual_sum >= 0:
            p_val_one_sided = p_val_two_sided / 2
        else:
            p_val_one_sided = 1 - (p_val_two_sided / 2)

        if verbose:
            print(f"test_statistic: {test_statistic}\n" \
                  f"p_val_two_sided: {p_val_two_sided}\n" \
                  f"diagonal_residual_sum: {diagonal_residual_sum}\n" \
                  f"p_val_one_sided: {p_val_one_sided}\n")

        return p_val_one_sided, odds_ratio

    def single_interval(self, edge, record_a, record_b, verbose=False):
        pdD = edge['p_d_D']
        geneA_samples = len(record_a['samples'])
        geneB_samples = len(record_b['samples'])
        geneAB_samples = len(edge['inter'])

        # observed
        O11 = geneAB_samples
        O12 = geneA_samples - geneAB_samples
        O21 = geneB_samples - geneAB_samples
        O22 = self.total_samples - geneA_samples - geneB_samples + geneAB_samples
        obs = [O11, O12, O21, O22]

        # expected
        E11 = (geneA_samples + geneB_samples - geneAB_samples) * pdD
        E12 = geneA_samples * (1 - pdD)
        E21 = geneB_samples * (1 - pdD)
        E22 = self.total_samples - (E11 + E12 + E21)
        exp = [E11, E12, E21, E22]

        if verbose:
            print(f"pdD value: {pdD}\n" \
                  f"{record_a['label']} samples: {geneA_samples}\n" \
                  f"{record_b['label']} samples: {geneB_samples}\n" \
                  f"{record_a['label']} & {record_b['label']} samples: {geneAB_samples}\n" \
                  f"Total samples: {self.total_samples}\n" \
                  f"Observed counts ([O11, O12, O21, O22]): {obs}\n" \
                  f"Expected counts ([E11, E12, E21, E22]): {exp}\n")

        return self.chi_squared_helper(obs, exp, verbose=verbose)

    def multi_interval(self, edge, record_a, record_b, verbose=False):
        # M_SAME_CHR = 0.275405

        pdD = edge['p_d_D']
        geneA_samples = len(record_a['samples'])
        geneB_samples = len(record_b['samples'])
        geneAB_samples = len(edge['inter'])

        # observed
        O11 = geneAB_samples
        O12 = geneA_samples - geneAB_samples
        O21 = geneB_samples - geneAB_samples
        O22 = self.total_samples - geneA_samples - geneB_samples + geneAB_samples
        obs = [O11, O12, O21, O22]

        # expected
        E11 = (geneA_samples) * (geneB_samples) * (1 - pdD) / self.total_samples
        # E11 = (geneA_samples) * (geneB_samples) * ((1-pdD)**2) / self.total_samples
        # E11 = E11 * M_SAME_CHR
        E12 = geneA_samples * (self.total_samples - geneB_samples) / self.total_samples
        E21 = geneB_samples * (self.total_samples - geneA_samples) / self.total_samples
        E22 = self.total_samples - (E11 + E12 + E21)
        exp = [E11, E12, E21, E22]

        if verbose:
            print(f"pdD value: {pdD}\n" \
                  f"{record_a['label']} samples: {geneA_samples}\n" \
                  f"{record_b['label']} samples: {geneB_samples}\n" \
                  f"{record_a['label']} & {record_b['label']} samples: {geneAB_samples}\n" \
                  f"Total samples: {self.total_samples}\n" \
                  f"Observed counts ([O11, O12, O21, O22]): {obs}\n" \
                  f"Expected counts ([E11, E12, E21, E22]): {exp}\n")

        return self.chi_squared_helper(obs, exp, verbose=verbose)

    def multi_chromosomal(self, edge, record_a, record_b, verbose=False):
        M_MULTI_CHR = 0.116055

        # pdD = edge['p_d_D']
        geneA_samples = len(record_a['samples'])
        geneB_samples = len(record_b['samples'])
        geneAB_samples = len(edge['inter'])

        # observed
        O11 = geneAB_samples
        O12 = geneA_samples - geneAB_samples
        O21 = geneB_samples - geneAB_samples
        O22 = self.total_samples - geneA_samples - geneB_samples + geneAB_samples
        obs = [O11, O12, O21, O22]

        # expected
        E11 = (geneA_samples) * (geneB_samples) * (M_MULTI_CHR) / self.total_samples
        E12 = geneA_samples * (self.total_samples - geneB_samples) / self.total_samples
        E21 = geneB_samples * (self.total_samples - geneA_samples) / self.total_samples
        E22 = self.total_samples - (E11 + E12 + E21)
        exp = [E11, E12, E21, E22]

        if verbose:
            print(f"Multichromosomal rate: {M_MULTI_CHR}\n" \
                  f"{record_a['label']} samples: {geneA_samples}\n" \
                  f"{record_b['label']} samples: {geneB_samples}\n" \
                  f"{record_a['label']} & {record_b['label']} samples: {geneAB_samples}\n" \
                  f"Total samples: {self.total_samples}\n" \
                  f"Observed counts ([O11, O12, O21, O22]): {obs}\n" \
                  f"Expected counts ([E11, E12, E21, E22]): {exp}\n")

        return self.chi_squared_helper(obs, exp, verbose=verbose)

    def multi_ecdna(self, edge, record_a, record_b, verbose=False):
        # to complete
        return -1, -1

    def q_val(self, p_values, alpha=0.05):
        valid_mask = [p != -1 for p in p_values]
        valid_p_values = [p for p in p_values if p != -1]
        # apply FDR correction only to valid p-values
        _, valid_q_values = fdrcorrection(valid_p_values, alpha=alpha)
        # reconstruct q_values with 'N/A' in the appropriate positions
        valid_q_iter = iter(valid_q_values)
        q_values = [next(valid_q_iter) if valid else -1 for valid in valid_mask]

        return q_values

    def find_edge_helper(self, source, target):
        """
        For test_dry_run() and test_selector_dry_run()
        """
        if source not in self.name_to_record:
            print(f"{source} not found.")
            return
        if target not in self.name_to_record:
            print(f"{target} not found.")
            return
        edge_name = f"{source}-{target}"
        if edge_name not in self.name_to_edge:
            print(f"No co-amplification found between {source} and {target}.")
            return
        return self.name_to_edge[edge_name]

    def test_dry_run(self, source, target, test=0):
        edge = self.find_edge_helper(source, target)
        if not edge:
            return

        p_val, q_val = edge['p_values'][test], edge['q_values'][test]
        sn, tn = self.name_to_record[edge['source']], self.name_to_record[edge['target']]
        print(f"{source} location: {sn['location']}\n" \
              f"{target} location: {tn['location']}\n" \
              f"Distance: {edge['distance']}")
        if p_val == -1:
            print("The selected test does not apply to this co-amplification.")
            return
        else:
            print(f"Stored p-value: {p_val}\nStored q-value: {q_val}\n")

        # match-case
        if test == 0:
            result = self.single_interval(edge, sn, tn, verbose=True)
        elif test == 1:
            result = self.multi_interval(edge, sn, tn, verbose=True)
        elif test == 2:
            result = self.multi_chromosomal(edge, sn, tn, verbose=True)
        elif test == 3:
            result = self.multi_ecdna(edge, sn, tn, verbose=True)
        else:
            return "Input a valid test selection"

        print("Final p-value: ", result[0])

    def test_selection_dry_run(self, source, target):
        edge = self.find_edge_helper(source, target)
        if not edge:
            return

        self.perform_tests(edge, verbose=True)

    def export_intervals(self, output_file="intervals.json"):
        """
        Export intervals from self.nodes into a JSON file for comparison with diff
        """
        export_data = {}

        for node in self.nodes:  # nodes are your updated records
            node_id = node.get("label")
            if not node_id:
                continue

            intervals = node.get("intervals", {})
            export_data[node_id] = {}

            for sample, feats in intervals.items():
                clean_feats = {k: v for k, v in feats.items() if v is not None}
                if clean_feats:  # only include non-empty samples
                    export_data[node_id][sample] = clean_feats

            # optionally remove node if it has no samples left
            if not export_data[node_id]:
                del export_data[node_id]

        # Write JSON, sort keys so diff is clean
        with open(output_file, "w") as f:
            json.dump(export_data, f, indent=2, sort_keys=True)

        print(f"Exported {len(export_data)} nodes' intervals to {output_file}")

    # get functions
    # -------------
    def Locs(self):
        return self.locs_by_genome

    def NumNodes(self):
        try:
            return len(self.nodes)
        except:
            print('Error: build graph')

    def NumEdges(self):
        try:
            return len(self.edges)
        except:
            print('Error: build graph')

    def Nodes(self):
        try:
            return self.nodes
        except:
            print('Error: build graph')

    def Edges(self):
        try:
            return self.edges
        except:
            print('Error: build graph')

    def Nodes_df(self):
        try:
            return self.nodes_df
        except:
            print('Error: build graph')

    def Edges_df(self):
        try:
            return self.edges_df
        except:
            print('Error: build graph')

