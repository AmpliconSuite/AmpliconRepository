import re
from tabnanny import verbose

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
        # processing parameters
        self.MERGE_CUTOFF = merge_cutoff
        self.pdD_MODEL = pdD_model

        # graph properties
        self.nodes = []
        self.edges = []
        self.gene_records = {}
        self.name_to_record = {}
        self.name_to_edge = {}
        self.reference_genome = None  # this is set in preprocess_dataset
        self.total_samples = 0
        self.preprocessed_dataset = None

        if dataset is None:
            print("ERROR: No dataset provided to Graph constructor")
            return

        # Preprocess dataset (includes reference check and normalization)
        preprocessed_dataset = self.preprocess_dataset(dataset, focal_amp)

        # Check if preprocessing failed
        if preprocessed_dataset is None:
            print("ERROR: Preprocessing failed. Constructing empty graph.")
            return

        self.total_samples = len(preprocessed_dataset)
        self.preprocessed_dataset = preprocessed_dataset

        # reference_genome is now normalized (either 'hg19' or 'hg38')
        ref_version = self.reference_genome

        # Map normalized reference to BED file
        ref_to_bed = {
            'hg38': 'hg38_genes.bed',
            'hg19': 'hg19_genes.bed',
        }

        if ref_version not in ref_to_bed:
            print(f"ERROR: Unsupported normalized reference genome: {ref_version}")
            print("Constructing empty graph.")
            return

        # Get BED file path for this reference
        bed_file = self.get_gene_bed_path(ref_version)

        # Check if BED file exists
        if not os.path.exists(bed_file):
            print(f"ERROR: BED file not found: {bed_file}")
            print("Constructing empty graph.")
            return

        # Load gene locations and names from BED file
        try:
            load_start = time.time()
            self.gene_records, self.name_to_record = self.create_gene_records_from_bed(bed_file)
            print(f"Loaded {len(self.gene_records)} genes from {ref_version} in {time.time() - load_start:.2f} seconds")
            print(f"Total gene name variants: {len(self.name_to_record)}")

            loc_count = sum([len(v['location']) > 0 for v in self.gene_records.values()])
            print(f"Genes with location data: {loc_count}")
        except Exception as e:
            print(f"ERROR: Failed to load gene records: {e}")
            print("Constructing empty graph.")
            return

        # create nodes and edges from the combined dataset
        if construct_graph:
            try:
                self.create_nodes(preprocessed_dataset)
                self.create_edges(by_sample)
            except Exception as e:
                print(f"ERROR: Failed to construct graph: {e}")
                print("Graph construction incomplete.")
                return

    def is_valid(self):
        """
        Check if the graph was successfully constructed

        Returns:
            bool: True if graph has nodes and was properly initialized
        """
        return (self.reference_genome is not None and
                len(self.gene_records) > 0)

    def normalize_chr(self, chrom):
        # Normalize chromosome names to always have 'chr' prefix
        chrom = str(chrom)
        if not chrom.startswith('chr'):
            return 'chr' + chrom
        return chrom

    def get_gene_bed_path(self, reference_genome):
        """
        Get the appropriate gene annotation bed file path based on reference genome

        Parameters:
            reference_genome (str) : Reference genome version (normalized: hg19 or hg38)
        Return:
            str : Path to the appropriate gene annotation bed file
        """
        caper_root = os.environ.get('CAPER_ROOT', '')
        bed_dir = os.path.join(caper_root, 'caper', 'bed_files')

        # Map normalized reference genome to appropriate bed file
        genome_to_bed = {
            'hg38': 'hg38_genes.bed',
            'hg19': 'hg19_genes.bed',
        }

        # Get bed file name
        bed_file = genome_to_bed.get(reference_genome)

        if bed_file is None:
            raise ValueError(f"No BED file mapping for reference: {reference_genome}")

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
            gene_coords['chr_normalized'] = gene_coords[0].apply(self.normalize_chr)

            # Convert to dictionary
            locs = dict(zip(gene_coords[3],
                            zip(gene_coords['chr_normalized'],
                                gene_coords[1].astype(int),
                                gene_coords[2].astype(int))))
        except Exception as e:
            print(f"Error importing gene coordinates from {bed_file}: {e}")

        return locs

    def create_gene_records_from_bed(self, bed_file):
        """
        Create gene records directly from BED file.
        BED format: chr  start  end  gene_name  .  strand  transcript_id

        Parameters:
            bed_file (str): Path to BED file

        Returns:
            gene_records (dict): {gene_name: record_dict}
            name_to_record (dict): {gene_name: record_dict} (includes all variants)
        """
        gene_records = {}

        try:
            bed_data = pd.read_csv(bed_file, sep="\t", header=None, comment="#")
            bed_data['chr_normalized'] = bed_data[0].apply(self.normalize_chr)

            # Group by gene name to handle duplicates
            # Multiple rows per gene can exist (different transcripts)
            for gene_name, group in bed_data.groupby(3):
                # Use the first entry for location (or could merge/extend ranges)
                first_row = group.iloc[0]
                chrom = first_row['chr_normalized']
                start = int(first_row[1])
                end = int(first_row[2])

                # Collect all transcript IDs for this gene (column 6)
                transcript_ids = set(group[6].dropna().astype(str))

                # Create gene record
                gene_records[gene_name] = {
                    'label': gene_name,
                    'all_labels': {gene_name} | transcript_ids,  # Gene name + all transcript IDs
                    'oncogene': '',
                    'features': set(),
                    'samples': set(),
                    'location': (chrom, start, end),
                    'intervals': defaultdict(lambda: defaultdict(int))
                }

            # Create retrieval index - map all names/IDs to their records
            name_to_record = {}
            for gene_name, record in gene_records.items():
                for name in record['all_labels']:
                    name_to_record[name] = record

        except Exception as e:
            print(f"Error loading gene records from {bed_file}: {e}")
            raise

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
        # Define equivalent reference groups (normalize to most common name)
        ref_equivalence = {
            'hg19': 'hg19',
            'GRCh37': 'hg19',
            'hg38': 'hg38',
            'GRCh38': 'hg38',
            'GRCh38_viral': 'hg38',
        }

        # Check that all data is from compatible reference genomes
        unique_refs = dataset['Reference_version'].unique()
        normalized_refs = set()

        for ref in unique_refs:
            if ref not in ref_equivalence:
                print(f"ERROR: Unsupported reference genome: {ref}")
                print(f"Supported references: {list(ref_equivalence.keys())}")
                return None
            normalized_refs.add(ref_equivalence[ref])

        if len(normalized_refs) > 1:
            print(f"ERROR: Incompatible reference genomes detected: {list(unique_refs)}")
            print(f"Normalized to: {list(normalized_refs)}")
            print(f"Data must be from compatible references (all GRCh37/hg19 or all GRCh38/hg38/GRCh38_viral)")
            return None

        self.reference_genome = list(normalized_refs)[0]  # Store the normalized reference
        print(f"Using reference genome: {self.reference_genome} (input references: {list(unique_refs)})")

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

        # return relevant columns
        preprocessed_dataset = filtered_dataset[['Sample_name',
                                                 'Feature_ID',
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

                self.edges.append({'source': record_a['label'],
                                   'target': record_b['label'],
                                   'weight': weight,
                                   'inter': inter,
                                   'union': union,
                                   'distance': distance,
                                   'p_d_D': -1,  # Will be computed in batch below
                                   'p_values': [-1] * 4,
                                   'odds_ratios': [-1] * 4,
                                   'q_values': [-1] * 4
                                   })
        print(
            f"Constructing edges took {time.time() - construct_start:.4f} seconds: {len(self.edges)} edges with non-empty intersections")

        # Vectorized p_d_D calculation
        pdd_start = time.time()
        self._compute_p_d_D_vectorized()
        print(f"Computing p_d_D values took {time.time() - pdd_start:.4f} seconds")

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

    def _compute_p_d_D_vectorized(self):
        """
        Compute p_d_D values for all edges using vectorized gamma.cdf
        """
        if not self.edges:
            return

        # Extract distances
        distances = np.array([edge['distance'] for edge in self.edges])

        # Get model parameters
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

        # Vectorized computation - only for valid distances (>= 0)
        valid_mask = distances >= 0
        p_d_D_values = np.full(len(distances), -1.0)  # Initialize with -1

        if np.any(valid_mask):
            valid_distances = distances[valid_mask]
            cdfs = gamma.cdf(valid_distances, a=params[0], loc=params[1], scale=params[2])
            p_d_D_values[valid_mask] = 1 - cdfs

        # Assign back to edges
        for i, edge in enumerate(self.edges):
            edge['p_d_D'] = p_d_D_values[i]

    def perform_tests(self, edge, verbose=False):
        """
        Perform all applicable significance tests and fill corresponding properties
        Fill lists in order:
            [0] single interval
            [1] multi interval
            [2] multi chromosomal
            [3] multi ecDNA
        """
        record_a = self.name_to_record[edge['source']]
        record_b = self.name_to_record[edge['target']]

        # Early return if missing location data
        if not record_a['location'] or not record_b['location']:
            return

        # Mark if missing interval data
        if (record_a.get('genes_with_chr_no_interval_counter') or
                record_a.get('genes_with_no_chr_match_counter') or
                record_b.get('genes_with_chr_no_interval_counter') or
                record_b.get('genes_with_no_chr_match_counter')):
            edge['missing_interval_data'] = True

        # Pre-compute chromosome compatibility (constant for all samples)
        same_chr = record_a['location'][0] == record_b['location'][0]

        # Classify co-amplifications by type
        coamp_counts = self._classify_coamplifications(edge, record_a, record_b, same_chr)

        # Determine which tests are applicable by checking ALL samples
        applicable_tests, log_data = self._determine_applicable_tests(edge, record_a, record_b, same_chr, verbose)

        # Prepare verbose logging
        log = []
        if verbose:
            log.append(f"{len(edge['inter'])} shared samples\n")
            log.append(f"Co-amplification counts: {coamp_counts}\n")
            log.append(f"Missing interval data: {'missing_interval_data' in edge}\n")
            log.append(f"same_chr: {same_chr}\n")
            if log_data:
                log.extend(log_data)

        # Execute only the applicable tests (no loops, no conditions)
        if applicable_tests[0]:  # single interval
            p_value, odds_ratio = self.single_interval(edge, record_a, record_b, coamp_counts)
            edge['p_values'][0] = p_value
            edge['odds_ratios'][0] = odds_ratio
            if verbose:
                log.append(f"Ran single_interval test: p={p_value:.3g}, OR={odds_ratio:.3g}\n")

        if applicable_tests[1]:  # multi interval
            p_value, odds_ratio = self.multi_interval(edge, record_a, record_b, coamp_counts)
            edge['p_values'][1] = p_value
            edge['odds_ratios'][1] = odds_ratio
            if verbose:
                log.append(f"Ran multi_interval test: p={p_value:.3g}, OR={odds_ratio:.3g}\n")

        if applicable_tests[2]:  # multi chromosomal
            p_value, odds_ratio = self.multi_chromosomal(edge, record_a, record_b, coamp_counts)
            edge['p_values'][2] = p_value
            edge['odds_ratios'][2] = odds_ratio
            if verbose:
                log.append(f"Ran multi_chromosomal test: p={p_value:.3g}, OR={odds_ratio:.3g}\n")

        if applicable_tests[3]:  # multi ecDNA
            p_value, odds_ratio = self.multi_ecdna(edge, record_a, record_b, coamp_counts)
            edge['p_values'][3] = p_value
            edge['odds_ratios'][3] = odds_ratio
            if verbose:
                log.append(f"Ran multi_ecDNA test: p={p_value:.3g}, OR={odds_ratio:.3g}\n")

        if verbose:
            print("\n".join(log))

    def _classify_coamplifications(self, edge, record_a, record_b, same_chr):
        """
        Classify each co-amplification by type

        Returns:
            dict with counts for each test type
        """
        counts = {
            'single_interval': 0,  # same chr, same feature, same interval
            'multi_interval': 0,  # same chr, same feature, diff interval
            'multi_chromosomal': 0,  # diff chr
            'multi_ecdna': 0,  # diff feature
            'total': len(edge['inter'])
        }

        samples = edge['inter']
        intervals_a_all = record_a['intervals']
        intervals_b_all = record_b['intervals']

        for sample in samples:
            intervals_a = intervals_a_all[sample]
            intervals_b = intervals_b_all[sample]
            features_ab = set(intervals_a.keys()) & set(intervals_b.keys())

            # Different chromosomes
            if not same_chr:
                counts['multi_chromosomal'] += 1
                if not features_ab:  # Also different features
                    counts['multi_ecdna'] += 1
                continue

            # Same chromosome, different features
            if not features_ab:
                counts['multi_ecdna'] += 1
                continue

            # Same chromosome, same feature - check intervals
            intervals_to_check = [(intervals_a[f], intervals_b[f])
                                  for f in features_ab
                                  if intervals_a[f] is not None and intervals_b[f] is not None]

            if intervals_to_check:
                if any(a == b for a, b in intervals_to_check):
                    counts['single_interval'] += 1

                #TODO: If two genes are on different intervals, but genes are close (<100kbp), should it be multi_interval?
                elif any(a != b for a, b in intervals_to_check):
                    counts['multi_interval'] += 1

        return counts

    # TODO: Determine if this function is necessary, given the coamp_counts genration in the function above
    def _determine_applicable_tests(self, edge, record_a, record_b, same_chr, verbose=False):
        """
        Determine which tests apply by scanning all samples once.

        Returns:
            tuple: (applicable_tests, log_data)
                applicable_tests: list[bool] - [single_interval, multi_interval, multi_chromosomal, multi_ecDNA]
                log_data: list[str] - verbose logging information
        """
        applicable = [False, False, False, False]
        log_data = []

        # For same chromosome, check samples to determine other tests
        samples = edge['inter']
        intervals_a_all = record_a['intervals']
        intervals_b_all = record_b['intervals']

        found_same_interval = False
        found_diff_interval_same_feature = False
        found_diff_feature = False
        first_sample_with_diff_feature = None

        for sample in samples:
            intervals_a = intervals_a_all[sample]
            intervals_b = intervals_b_all[sample]
            features_ab = set(intervals_a.keys()) & set(intervals_b.keys())

            # Check for different features (multi ecDNA)
            if not features_ab:
                if not found_diff_feature:
                    found_diff_feature = True
                    first_sample_with_diff_feature = sample
                # Continue checking for other conditions (don't early exit)
                if not same_chr:
                    # If different chromosomes, we can exit early after finding diff_feature
                    if found_diff_feature:
                        break
                else:
                    # If same chromosome, still need to check for interval conditions
                    if found_same_interval and found_diff_interval_same_feature:
                        break
                continue

            # Only check intervals if on same chromosome
            if same_chr:
                # Check intervals within shared features
                intervals_to_check = [(intervals_a[f], intervals_b[f])
                                      for f in features_ab
                                      if intervals_a[f] is not None and intervals_b[f] is not None]

                if intervals_to_check:
                    has_same = any(a == b for a, b in intervals_to_check)
                    has_diff = any(a != b for a, b in intervals_to_check)

                    if has_same and not found_same_interval:
                        found_same_interval = True
                        first_sample_with_same_interval = sample
                        if verbose:
                            log_data.append(f"Sample '{sample}': same_interval=True, features={sorted(features_ab)}\n")

                    #TODO: Determine if we need to consider two genes found on different intervals, but reference distance is close (<100kbp)
                    if has_diff and not found_diff_interval_same_feature:
                        found_diff_interval_same_feature = True
                        first_sample_with_diff_interval = sample
                        if verbose:
                            log_data.append(f"Sample '{sample}': diff_interval=True, features={sorted(features_ab)}\n")

                # Early exit if we found all conditions for same chromosome
                if found_same_interval and found_diff_interval_same_feature and found_diff_feature:
                    break

        # Set applicable tests based on what we found
        applicable[0] = found_same_interval and same_chr  # single interval (only same chr)
        applicable[1] = found_diff_interval_same_feature and same_chr  # multi interval (only same chr)
        applicable[2] = not same_chr  # multi chromosomal (only diff chr)
        applicable[3] = found_diff_feature  # multi ecDNA (any chromosome)

        if verbose:
            log_data.append(f"same_chr: {same_chr}\n")
            if found_diff_feature:
                log_data.append(f"Sample '{first_sample_with_diff_feature}': different features (multi_ecDNA)\n")
            if not same_chr:
                log_data.append("Different chromosomes - multi_chromosomal test applies\n")

        return applicable, log_data

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

    def single_interval(self, edge, record_a, record_b, coamp_counts, verbose=False):
        pdD = edge['p_d_D']
        geneA_samples = len(record_a['samples'])
        geneB_samples = len(record_b['samples'])
        total_coamps = len(edge['inter'])

        # observed
        O11 = coamp_counts['single_interval']
        nonapplicable_coamps = total_coamps - O11

        geneA_alone = geneA_samples - total_coamps
        geneB_alone = geneB_samples - total_coamps

        O12 = geneA_alone + 0.5 * nonapplicable_coamps
        O21 = geneB_alone + 0.5 * nonapplicable_coamps
        O22 = self.total_samples - O11 - O12 - O21
        obs = [O11, O12, O21, O22]

        # expected
        E11 = (geneA_samples + geneB_samples - O11) * pdD
        E12 = geneA_samples * (1 - pdD)
        E21 = geneB_samples * (1 - pdD)
        E22 = self.total_samples - (E11 + E12 + E21)
        exp = [E11, E12, E21, E22]

        if verbose:
            print(f"pdD value: {pdD}\n" \
                  f"{record_a['label']} samples: {geneA_samples}\n" \
                  f"{record_b['label']} samples: {geneB_samples}\n" \
                  f"{record_a['label']} & {record_b['label']} (same interval): {O11}\n" \
                  f"Total co-amplifications: {total_coamps}\n" \
                  f"Non-applicable co-amps: {nonapplicable_coamps}\n" \
                  f"Total samples: {self.total_samples}\n" \
                  f"Observed counts ([O11, O12, O21, O22]): {obs}\n" \
                  f"Expected counts ([E11, E12, E21, E22]): {exp}\n")

        return self.chi_squared_helper(obs, exp, verbose=verbose)

    def multi_interval(self, edge, record_a, record_b, coamp_counts, verbose=False):
        pdD = edge['p_d_D']
        geneA_samples = len(record_a['samples'])
        geneB_samples = len(record_b['samples'])
        total_coamps = len(edge['inter'])

        # observed
        O11 = coamp_counts['multi_interval']
        nonapplicable_coamps = total_coamps - O11

        geneA_alone = geneA_samples - total_coamps
        geneB_alone = geneB_samples - total_coamps

        O12 = geneA_alone + 0.5 * nonapplicable_coamps
        O21 = geneB_alone + 0.5 * nonapplicable_coamps
        O22 = self.total_samples - O11 - O12 - O21
        obs = [O11, O12, O21, O22]

        # expected
        E11 = (geneA_samples) * (geneB_samples) * (1 - pdD) / self.total_samples
        E12 = geneA_samples * (self.total_samples - geneB_samples) / self.total_samples
        E21 = geneB_samples * (self.total_samples - geneA_samples) / self.total_samples
        E22 = self.total_samples - (E11 + E12 + E21)
        exp = [E11, E12, E21, E22]

        if verbose:
            print(f"pdD value: {pdD}\n" \
                  f"{record_a['label']} samples: {geneA_samples}\n" \
                  f"{record_b['label']} samples: {geneB_samples}\n" \
                  f"{record_a['label']} & {record_b['label']} (multi interval): {O11}\n" \
                  f"Total co-amplifications: {total_coamps}\n" \
                  f"Non-applicable co-amps: {nonapplicable_coamps}\n" \
                  f"Total samples: {self.total_samples}\n" \
                  f"Observed counts ([O11, O12, O21, O22]): {obs}\n" \
                  f"Expected counts ([E11, E12, E21, E22]): {exp}\n")

        return self.chi_squared_helper(obs, exp, verbose=verbose)

    def multi_chromosomal(self, edge, record_a, record_b, coamp_counts, verbose=False):
        M_MULTI_CHR = 0.116055

        geneA_samples = len(record_a['samples'])
        geneB_samples = len(record_b['samples'])
        total_coamps = len(edge['inter'])

        # observed
        O11 = coamp_counts['multi_chromosomal']
        nonapplicable_coamps = total_coamps - O11

        geneA_alone = geneA_samples - total_coamps
        geneB_alone = geneB_samples - total_coamps

        O12 = geneA_alone + 0.5 * nonapplicable_coamps
        O21 = geneB_alone + 0.5 * nonapplicable_coamps
        O22 = self.total_samples - O11 - O12 - O21
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
                  f"{record_a['label']} & {record_b['label']} (multi chromosomal): {O11}\n" \
                  f"Total co-amplifications: {total_coamps}\n" \
                  f"Non-applicable co-amps: {nonapplicable_coamps}\n" \
                  f"Total samples: {self.total_samples}\n" \
                  f"Observed counts ([O11, O12, O21, O22]): {obs}\n" \
                  f"Expected counts ([E11, E12, E21, E22]): {exp}\n")

        return self.chi_squared_helper(obs, exp, verbose=verbose)

    def multi_ecdna(self, edge, record_a, record_b, coamp_counts, verbose=False):
        geneA_samples = len(record_a['samples'])
        geneB_samples = len(record_b['samples'])
        total_coamps = len(edge['inter'])

        # observed
        O11 = coamp_counts['multi_ecdna']
        nonapplicable_coamps = total_coamps - O11

        geneA_alone = geneA_samples - total_coamps
        geneB_alone = geneB_samples - total_coamps

        O12 = geneA_alone + 0.5 * nonapplicable_coamps
        O21 = geneB_alone + 0.5 * nonapplicable_coamps
        O22 = self.total_samples - O11 - O12 - O21
        obs = [O11, O12, O21, O22]

        # expected - TODO: need proper expected value model
        # For now, return -1 to indicate not implemented
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

        # Classify co-amplifications to get counts
        same_chr = sn['location'][0] == tn['location'][0]
        coamp_counts = self._classify_coamplifications(edge, sn, tn, same_chr)
        print(f"Co-amplification counts: {coamp_counts}\n")

        # match-case
        if test == 0:
            result = self.single_interval(edge, sn, tn, coamp_counts, verbose=True)
        elif test == 1:
            result = self.multi_interval(edge, sn, tn, coamp_counts, verbose=True)
        elif test == 2:
            result = self.multi_chromosomal(edge, sn, tn, coamp_counts, verbose=True)
        elif test == 3:
            result = self.multi_ecdna(edge, sn, tn, coamp_counts, verbose=True)
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

