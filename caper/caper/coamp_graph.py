import re
import pandas as pd
import numpy as np
import networkx as nx
from collections import defaultdict
import time
import os
from scipy.stats import expon
from scipy.stats import gamma
from scipy.stats import chi2
from statsmodels.stats.multitest import fdrcorrection

class Graph:

	def __init__(self, dataset=None, focal_amp="ecDNA", by_sample=False, merge_cutoff=50000, pdD_model='gamma_random_breakage', construct_graph=True):
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
			
			start_time = time.time()
			# define reference genomes to process
			ref_genomes = ['hg38_gtf', 'hg19_gtf']
			base_ref = 'GRCh38'

			ref_files = [self.get_gene_bed_path(ref) for ref in ref_genomes]
			base_ref_file = self.get_gene_bed_path(base_ref)

			# load gene location data for base reference
			locs_by_base_ref = self.import_locs(base_ref_file)
			print(f"Retrieved locations for {len(locs_by_base_ref)} {base_ref} genes in {time.time() - start_time:.2f} seconds")

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

			# Vectorized operation for chromosome naming
			gene_coords['chr_num'] = gene_coords[0].str.extract(r'chr(\w+)', expand=False)
            
			# Convert to dictionary in one operation instead of row-by-row
			locs = dict(zip(gene_coords[3],
							zip(gene_coords['chr_num'],
								gene_coords[1].astype(int),
								gene_coords[2].astype(int))))
		except Exception as e:
			print(f"Error importing gene coordinates from {bed_file}: {e}")

		return locs

	def create_gene_records(self, ref_files, locs_by_base_ref):
		gene_records = {}
		# parse name and id data
		names_ids = pd.DataFrame(columns = ['ID', 'Name'])
		for ref in ref_files:
			df = pd.read_csv(ref, sep="\t", comment="#", header=None)
			df = df.assign(Name = lambda x: (x[8].str.extract(r'Name=([^;]+)')))
			df = df.assign(ID = lambda x: (x[8].str.extract(r'Accession=([^;]+)')))
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

		# find all matches of chr:start-end
		matches = re.findall(r"'?([\dXY]+):(\d+)-(\d+)'?", location)
		matches = [[chrom, int(start), int(end)] for chrom, start, end in matches]

		if not matches:
			return matches

		curr_interval = matches[0]
		for i in range(len(matches)):            
			# if end is reached, add the current interval
			if i == len(matches) - 1:
				merged_intervals.append(curr_interval)
			# if this interval can be merged with the next, extend the current interval
			elif matches[i][0] == matches[i+1][0] and matches[i+1][1] - matches[i][2] <= self.MERGE_CUTOFF:
				curr_interval[2] = matches[i+1][2]
			# otherwise add the current interval and reset
			else:
				merged_intervals.append(curr_interval)
				curr_interval = matches[i+1]

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
		# replace spaces with underscores (for testing locally downloaded datasets)
		# dataset.columns = dataset.columns.str.replace(' ', '_')

		# subset dataset by focal amplification type
		filter_start = time.time()
		filtered_dataset = dataset[dataset['Classification'] == focal_amp].copy()
		filter_end = time.time()
		print(f"Filtering features took {filter_end - filter_start:.4f} seconds, resulting in {len(filtered_dataset)} features")

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
		for __, row in dataset.iterrows():
			# get the properties in this feature
			oncogenes = row.get('Oncogenes')
			all_genes = row.get('All_genes')
			feature = row.get('Feature_ID')
			sample = row.get('Sample_name')
			intervals = row.get('Merged_Intervals')
			
			# store interval coordinates of feature
			interval_lookup = defaultdict(list)
			for interval in intervals:
				ichr, istart, iend = interval
				interval_lookup[ichr].append([istart, iend, interval_id_counter])
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
						chr, start, __ = record['location']
						if chr in interval_lookup: 
							interval_found = False
							for istart, iend, id in interval_lookup[chr]:
								if (start > istart and start < iend):
									genes_with_interval_counter += 1                               
									record['intervals'][sample][feature_id_counter] = id
									interval_found = True
									break
							if not interval_found:
								genes_with_chr_no_interval_counter += 1
								record['genes_with_chr_no_interval_counter'] = True
						else:
							genes_with_no_chr_match_counter += 1
							record['genes_with_no_chr_match_counter'] = True
							self.genes_with_no_chr_match_list.append((chr, interval_lookup.keys()))
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
		print(f"TEST: {genes_with_interval_counter} searches where gene's chr on interval and gene's location is matched to an interval")
		print(f"TEST: {genes_with_chr_no_interval_counter} searches where gene's chr on interval but gene's location is NOT matched to an interval ({test_size_1} unique nodes)")
		print(f"TEST: {genes_with_no_chr_match_counter} searches where gene's chr not on interval ({test_size_2} unique nodes)")

		process_end = time.time()
		print(
			f"Processing {gene_count} genes took {process_end - process_start:.4f} seconds, resulting in {len(self.nodes)} unique nodes")

		no_interval_count = sum([len(r['intervals']) == 0 for r in self.nodes])
		print(f"{no_interval_count} genes were not amplified on any merged intervals")

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
					pair = tuple(sorted((node1, node2))) # avoid duplicates
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
				
		# perform significance testing on edges
		p_start = time.time()
		na_counter = 0
		for edge in self.edges:
			self.perform_tests(edge)
			if edge['p_values'] == [-1,-1,-1,-1]:
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
			e_s = abs(record_a['location'][2] - record_a['location'][1])
			s_e = abs(record_a['location'][1] - record_a['location'][2])
			distance = min(e_s, s_e)
		return distance
	
	def p_d_D(self, distance):
		"""
		generate p_val based on naive random breakage model, considering distance
        but not number of incidents of co-amplification
		"""
		if distance < 0:
			return -1
		models = { 'gamma_random_breakage': [0.3987543483729932,
											 1.999999999999,
											 1749495.5696758535],
				   'gamma_capped_5m': [0.4528058380301946,
						               1.999999999999,
									   1192794.3765480835] }
		params = models[self.pdD_MODEL]
		cdf = gamma.cdf(distance, a=params[0], loc=params[1], scale=params[2])
		return 1 - cdf

	def perform_tests(self, edge):
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
		if record_a.get('genes_with_chr_no_interval_counter') or record_a.get('genes_with_no_chr_match_counter') or record_b.get('genes_with_chr_no_interval_counter') or record_b.get('genes_with_no_chr_match_counter'):
			edge['missing_interval_data'] = True

		samples = list(edge['inter'])
		tested = [False, False, False, False]

		# for each co-amplified sample, determine the type of co-amplification
		# and run the appropriate test if not previously done
		i = 0
		while i < len(samples) and False in tested:
			intervals_a = record_a['intervals'][samples[i]]
			intervals_b = record_b['intervals'][samples[i]]
			features_ab = set(intervals_a.keys()) & set(intervals_b.keys())

			if len(features_ab) > 1:
				print('Note: unexpected number of shared features in sample')

			same_chr = record_a['location'][0] == record_b['location'][0]
			same_feature = len(features_ab) > 0
			same_interval = any(intervals_a[f] == intervals_b[f] 
								for f in features_ab 
								if intervals_a[f] is not None 
								and intervals_b[f] is not None)
			diff_interval = any(intervals_a[f] != intervals_b[f] 
								for f in features_ab 
								if intervals_a[f] is not None 
								and intervals_b[f] is not None)

			# [0] single interval test
			if not tested[0] and same_interval:
				p_value, odds_ratio = self.single_interval(edge, record_a, record_b)
				edge['p_values'][0] = p_value
				edge['odds_ratios'][0] = odds_ratio
				tested[0] = True

			# [1] multi interval test
			if not tested[1] and same_chr and same_feature and diff_interval:
				p_value, odds_ratio = self.multi_interval(edge, record_a, record_b)
				edge['p_values'][1] = p_value
				edge['odds_ratios'][1] = odds_ratio
				tested[1] = True

			# [2] multi chromosomal test
			if not tested[2] and not same_chr:
				p_value, odds_ratio = self.multi_chromosomal(edge, record_a, record_b)
				edge['p_values'][2] = p_value
				edge['odds_ratios'][2] = odds_ratio
				tested[2] = True
			
			# [3] multi ecDNA test
			if not tested[3] and not same_feature:
				p_value, odds_ratio = self.multi_ecdna(edge, record_a, record_b)
				edge['p_values'][3] = p_value
				edge['odds_ratios'][3] = odds_ratio
				tested[3] = True

			i += 1

	def chi_squared_helper(self, obs, exp):
		# apply Haldane correction if any observed category count is zero
		if 0 in obs:
			obs = [o + 0.5 for o in obs]
			exp = [e + 0.5 for e in exp]

		total_obs = sum(obs)
		total_exp = sum(exp)
		obs_freq = [o / total_obs for o in obs]
		exp_freq = [e / total_exp for e in exp]

		test_statistic = sum([(o-e)*(o-e)/e for o,e in zip(obs_freq, exp_freq)])
		cdf = chi2.cdf(test_statistic, df=1)
		p_val_two_sided = 1-cdf
		diagonal_residual_sum = ((obs_freq[0]-exp_freq[0]) + (obs_freq[3]-exp_freq[3]))

		odds_ratio = obs_freq[0] / exp_freq[0]
		if odds_ratio > 1e9:
			odds_ratio = 1e9 # cap ultra large to prevent infinite odds ratios

		# convert to one-sided p-value
		if diagonal_residual_sum >= 0:
			p_val_one_sided = p_val_two_sided / 2
		else:
			p_val_one_sided = 1 - (p_val_two_sided / 2)

		return p_val_one_sided, odds_ratio

	def single_interval(self, edge, record_a, record_b):
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
		E22 = self.total_samples - (geneA_samples + geneB_samples - geneAB_samples)
		exp = [E11, E12, E21, E22] 

		return self.chi_squared_helper(obs, exp)

	def multi_interval(self, edge, record_a, record_b):
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
		E11 = (geneA_samples) * (geneB_samples) * ((1-pdD)**2) 
		# E11 = E11 * M_SAME_CHR
		E12 = geneA_samples * (self.total_samples - geneB_samples)
		E21 = geneB_samples * (self.total_samples - geneA_samples)
		E22 = (self.total_samples - geneA_samples) * (self.total_samples - geneB_samples)
		exp = [E11, E12, E21, E22] 

		return self.chi_squared_helper(obs, exp)

	def multi_chromosomal(self, edge, record_a, record_b):
		M_MULTI_CHR = 0.116055

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
		E11 = (geneA_samples) * (geneB_samples) * (M_MULTI_CHR)
		E12 = geneA_samples * (self.total_samples - geneB_samples)
		E21 = geneB_samples * (self.total_samples - geneA_samples)
		E22 = (self.total_samples - geneA_samples) * (self.total_samples - geneB_samples)
		exp = [E11, E12, E21, E22] 

		return self.chi_squared_helper(obs, exp)

	def multi_ecdna(self, edge, record_a, record_b):
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

	# # -------------------- (soon to be) deprecated functions -------------------

	# def CreateEdges(self):
	# 	"""
    #     Create edges, ensuring genes being connected share a reference genome
    #     """
	# 	start_time = time.time()
	# 	print(f"Starting CreateEdges with {len(self.nodes_df)} nodes")

	# 	# extract relevant columns
	# 	extract_start = time.time()
	# 	labels = self.nodes_df['label'].values
	# 	features = self.nodes_df['features'].values
	# 	references = self.nodes_df['reference_genomes'].values
	# 	extract_end = time.time()
	# 	print(f"Extracting columns took {extract_end - extract_start:.4f} seconds")

	# 	# build reverse index to map features to nodes
	# 	index_start = time.time()
	# 	feature_to_nodes = defaultdict(list)
	# 	# Pre-compute feature to node mapping in bulk
	# 	feature_node_pairs = [(amp, index)
	# 						  for index, feature_list in enumerate(features)
	# 						  for amp in feature_list]

	# 	# Build mapping in one pass
	# 	for amp, index in feature_node_pairs:
	# 		feature_to_nodes[amp].append(index)
	# 	index_end = time.time()
	# 	print(
	# 		f"Building feature index took {index_end - index_start:.4f} seconds with {len(feature_to_nodes)} unique features")

	# 	# collect potential node pairs, avoiding duplicates
	# 	pairs_start = time.time()
	# 	potential_pairs = set()
	# 	for nodelist in feature_to_nodes.values():
	# 		for i, node1 in enumerate(nodelist):
	# 			for node2 in nodelist[i + 1:]:
	# 				potential_pairs.add((node1, node2))
	# 	pairs_end = time.time()
	# 	print(
	# 		f"Collecting potential pairs took {pairs_end - pairs_start:.4f} seconds, found {len(potential_pairs)} pairs")

	# 	# Early return if no pairs
	# 	if not potential_pairs:
	# 		self.edges = []
	# 		self.edges_df = pd.DataFrame(self.edges)
	# 		print("No potential pairs found, returning early")
	# 		return

	# 	# unzip pairs into source and target nodes
	# 	pairs = list(potential_pairs)
	# 	src_indices, tgt_indices = zip(*pairs)

	# 	# validate pairs share a reference genome
	# 	validate_start = time.time()
	# 	valid_pairs = []
	# 	valid_src = []
	# 	valid_tgt = []

	# 	for s, t in zip(src_indices, tgt_indices):
	# 		src_refs = references[s]
	# 		tgt_refs = references[t]

	# 		# Check if they share at least one reference genome
	# 		if any(ref in tgt_refs for ref in src_refs):
	# 			valid_pairs.append((s, t))
	# 			valid_src.append(s)
	# 			valid_tgt.append(t)
	# 	validate_end = time.time()
	# 	print(
	# 		f"Validating reference genomes took {validate_end - validate_start:.4f} seconds, {len(valid_pairs)} valid pairs")

	# 	# Continue with only valid pairs
	# 	src_indices = valid_src
	# 	tgt_indices = valid_tgt

	# 	# Early return if no valid pairs
	# 	if not valid_src:
	# 		self.edges = []
	# 		self.edges_df = pd.DataFrame(self.edges)
	# 		print("No valid pairs found, returning early")
	# 		return

	# 	# retrieve the matching features for all source and target nodes
	# 	feature_start = time.time()
	# 	src_features = [features[i] for i in src_indices]
	# 	tgt_features = [features[j] for j in tgt_indices]

	# 	# remove id information from feature labels
	# 	clean_src_features = [[f.split("_amplicon")[0] for f in feature_list] for feature_list in src_features]
	# 	clean_tgt_features = [[f.split("_amplicon")[0] for f in feature_list] for feature_list in tgt_features]
	# 	feature_end = time.time()
	# 	print(f"Processing feature lists took {feature_end - feature_start:.4f} seconds")

	# 	# calculate all intersections and unions
	# 	set_start = time.time()
	# 	src_sets = [set(s) for s in clean_src_features]
	# 	tgt_sets = [set(t) for t in clean_tgt_features]
	# 	inters = [list(s & t) for s, t in zip(src_sets, tgt_sets)]
	# 	unions = [list(s | t) for s, t in zip(src_sets, tgt_sets)]
	# 	set_end = time.time()
	# 	print(f"Set operations took {set_end - set_start:.4f} seconds")

	# 	# filter pairs with non-empty intersections
	# 	filter_start = time.time()
	# 	non_empty_mask = [len(inter) > 0 for inter in inters]
	# 	src_filtered = [src_indices[i] for i, mask in enumerate(non_empty_mask) if mask]
	# 	tgt_filtered = [tgt_indices[i] for i, mask in enumerate(non_empty_mask) if mask]
	# 	inters_filtered = [inters[i] for i, mask in enumerate(non_empty_mask) if mask]
	# 	unions_filtered = [unions[i] for i, mask in enumerate(non_empty_mask) if mask]
	# 	src_sets_filtered = [src_sets[i] for i, mask in enumerate(non_empty_mask) if mask]
	# 	tgt_sets_filtered = [tgt_sets[i] for i, mask in enumerate(non_empty_mask) if mask]
	# 	filter_end = time.time()
	# 	print(
	# 		f"Filtering non-empty intersections took {filter_end - filter_start:.4f} seconds, {len(src_filtered)} pairs remain")

	# 	# Early return if no filtered pairs
	# 	if not src_filtered:
	# 		self.edges = []
	# 		self.edges_df = pd.DataFrame(self.edges)
	# 		print("No filtered pairs found, returning early")
	# 		return

	# 	# calculate weights
	# 	weight_start = time.time()
	# 	weights = [len(i) / len(u) for i, u in zip(inters_filtered, unions_filtered)]
	# 	weight_end = time.time()
	# 	print(f"Calculating weights took {weight_end - weight_start:.4f} seconds")

	# 	# compute p and q values
	# 	pval_start = time.time()
	# 	single_interval_results = [self.single_interval_test(labels[i], labels[j], src_sets_filtered[idx], tgt_sets_filtered[idx], inters_filtered[idx], self.total_samples) for idx, (i, j) in enumerate(zip(src_filtered, tgt_filtered))]
	# 	p_values_single_interval, odds_ratio_single_interval, distances = zip(*single_interval_results)
	# 	p_values_single_interval = list(p_values_single_interval)
	# 	odds_ratio_single_interval = list(odds_ratio_single_interval)
	# 	distances = list(distances)

	# 	multi_interval_results = [self.multi_interval_test(labels[i], labels[j], src_sets_filtered[idx], tgt_sets_filtered[idx], inters_filtered[idx], self.total_samples) for idx, (i, j) in enumerate(zip(src_filtered, tgt_filtered))]
	# 	p_values_multi_interval, odds_ratio_multi_interval, _ = zip(*multi_interval_results)
	# 	p_values_multi_interval = list(p_values_multi_interval)
	# 	odds_ratio_multi_interval = list(odds_ratio_multi_interval)

	# 	multi_chromosomal_results = [self.multi_chromosomal_test(labels[i], labels[j], src_sets_filtered[idx], tgt_sets_filtered[idx], inters_filtered[idx], self.total_samples) for idx, (i, j) in enumerate(zip(src_filtered, tgt_filtered))]
	# 	p_values_multi_chromosomal, odds_ratio_multi_chromosomal, _ = zip(*multi_chromosomal_results)
	# 	p_values_multi_chromosomal = list(p_values_multi_chromosomal)
	# 	odds_ratio_multi_chromosomal = list(odds_ratio_multi_chromosomal)
	# 	pval_end = time.time()
	# 	print(f"Calculating p-values took {pval_end - pval_start:.4f} seconds")

	# 	qval_start = time.time()
	# 	q_values_single_interval = self.QVal(p_values_single_interval)
	# 	q_values_multi_interval = self.QVal(p_values_multi_interval)
	# 	q_values_multi_chromosomal = self.QVal(p_values_multi_chromosomal)
	# 	qval_end = time.time()
	# 	print(f"Calculating q-values took {qval_end - qval_start:.4f} seconds")

	# 	# create edges dict and df
	# 	edges_start = time.time()
	# 	self.edges = [
	# 		{
	# 			'source': labels[i],
	# 			'target': labels[j],
	# 			'weight': weights[idx],
	# 			'inter': inters_filtered[idx],
	# 			'union': unions_filtered[idx],
	# 			'distance': distances[idx], 
	# 			'pval_single_interval': p_values_single_interval[idx],
	# 			'qval_single_interval': q_values_single_interval[idx],
	# 			'odds_ratio_single_interval': odds_ratio_single_interval[idx],
	# 			'pval_multi_interval': p_values_multi_interval[idx],
	# 			'qval_multi_interval': q_values_multi_interval[idx],
	# 			'odds_ratio_multi_interval': odds_ratio_multi_interval[idx],
	# 			'pval_multi_chromosomal': p_values_multi_chromosomal[idx],
	# 			'qval_multi_chromosomal': q_values_multi_chromosomal[idx],
	# 			'odds_ratio_multi_chromosomal': odds_ratio_multi_chromosomal[idx],
	# 		}
	# 		for idx, (i, j) in enumerate(zip(src_filtered, tgt_filtered))
	# 	]
	# 	self.edges_df = pd.DataFrame(self.edges)
	# 	edges_end = time.time()
	# 	print(f"Creating edges DataFrame took {edges_end - edges_start:.4f} seconds, {len(self.edges)} edges created")

	# 	total_time = time.time() - start_time
	# 	print(f"Total CreateEdges execution: {total_time:.4f} seconds")
	
	# def single_interval_test(self, gene_a, gene_b, src_sets_filtered, tgt_sets_filtered, inters_filtered, total_samples):
	# 	pdD, d = self.PVal(gene_a, gene_b)
	# 	if pdD == -1: return -1, -1, -1
			
	# 	geneA_samples = len(src_sets_filtered)
	# 	geneB_samples = len(tgt_sets_filtered)
	# 	geneAB_samples = len(inters_filtered)

	# 	# observed
	# 	O11 = geneAB_samples
	# 	O12 = geneA_samples - geneAB_samples
	# 	O21 = geneB_samples - geneAB_samples
	# 	O22 = total_samples - geneA_samples - geneB_samples + geneAB_samples
	# 	obs = [O11, O12, O21, O22]
		
	# 	# expected
	# 	E11 = (geneA_samples + geneB_samples - geneAB_samples) * pdD
	# 	E12 = geneA_samples * (1 - pdD)
	# 	E21 = geneB_samples * (1 - pdD)
	# 	E22 = total_samples - (geneA_samples + geneB_samples - geneAB_samples)
	# 	exp = [E11, E12, E21, E22] 

	# 	# apply Haldane correction if any observed category count is zero
	# 	if 0 in obs:
	# 		obs = [o + 0.5 for o in obs]
	# 		exp = [e + 0.5 for e in exp]

	# 	total_obs = sum(obs)
	# 	total_exp = sum(exp)
	# 	obs_freq = [o / total_obs for o in obs]
	# 	exp_freq = [e / total_exp for e in exp]

	# 	test_statistic = sum([(o-e)*(o-e)/e for o,e in zip(obs_freq, exp_freq)])
	# 	cdf = chi2.cdf(test_statistic, df=1)
	# 	p_val_two_sided = 1-cdf
	# 	diagonal_residual_sum = ((obs_freq[0]-exp_freq[0]) + (obs_freq[3]-exp_freq[3]))

	# 	odds_ratio = obs_freq[0] / exp_freq[0]
	# 	if odds_ratio > 1e9:
	# 		odds_ratio = 1e9 # cap ultra large to prevent infinite odds ratios

	# 	# convert to one-sided p-value
	# 	if diagonal_residual_sum >= 0:
	# 		p_val_one_sided = p_val_two_sided / 2
	# 	else:
	# 		p_val_one_sided = 1 - (p_val_two_sided / 2)

	# 	return p_val_one_sided, odds_ratio, d

	# def multi_interval_test(self, gene_a, gene_b, src_sets_filtered, tgt_sets_filtered, inters_filtered, total_samples, m_same_chromosome = 0.275405): 
	# 	pdD, d = self.PVal(gene_a, gene_b)
	# 	if pdD == -1: return -1, -1, -1
			
	# 	geneA_samples = len(src_sets_filtered)
	# 	geneB_samples = len(tgt_sets_filtered)
	# 	geneAB_samples = len(inters_filtered)

	# 	# observed
	# 	O11 = geneAB_samples
	# 	O12 = geneA_samples - geneAB_samples
	# 	O21 = geneB_samples - geneAB_samples
	# 	O22 = total_samples - geneA_samples - geneB_samples + geneAB_samples
	# 	obs = [O11, O12, O21, O22]
		
	# 	# expected
	# 	E11 = (geneA_samples) * (geneB_samples) * ((1-pdD)**2) * (m_same_chromosome)
	# 	E12 = geneA_samples * (total_samples - geneB_samples)
	# 	E21 = geneB_samples * (total_samples - geneA_samples)
	# 	E22 = (total_samples - geneA_samples) * (total_samples - geneB_samples)
	# 	exp = [E11, E12, E21, E22] 

	# 	# apply Haldane correction if any observed category count is zero
	# 	if 0 in obs:
	# 		obs = [o + 0.5 for o in obs]
	# 		exp = [e + 0.5 for e in exp]

	# 	total_obs = sum(obs)
	# 	total_exp = sum(exp)
	# 	obs_freq = [o / total_obs for o in obs]
	# 	exp_freq = [e / total_exp for e in exp]

	# 	test_statistic = sum([(o-e)*(o-e)/e for o,e in zip(obs_freq, exp_freq)])
	# 	cdf = chi2.cdf(test_statistic, df=1)
	# 	p_val_two_sided = 1-cdf
	# 	diagonal_residual_sum = ((obs_freq[0]-exp_freq[0]) + (obs_freq[3]-exp_freq[3]))

	# 	odds_ratio = obs_freq[0] / exp_freq[0]
	# 	if odds_ratio > 1e9:
	# 		odds_ratio = 1e9 # cap ultra large to prevent infinite odds ratios

	# 	# convert to one-sided p-value
	# 	if diagonal_residual_sum >= 0:
	# 		p_val_one_sided = p_val_two_sided / 2
	# 	else:
	# 		p_val_one_sided = 1 - (p_val_two_sided / 2)

	# 	return p_val_one_sided, odds_ratio, d
	
	# def multi_chromosomal_test(self, gene_a, gene_b, src_sets_filtered, tgt_sets_filtered, inters_filtered, total_samples, m_multi_chromosome = 0.116055):
	# 	pdD, d = self.PVal(gene_a, gene_b)
	# 	if pdD == -1: return -1, -1, -1
			
	# 	geneA_samples = len(src_sets_filtered)
	# 	geneB_samples = len(tgt_sets_filtered)
	# 	geneAB_samples = len(inters_filtered)

	# 	# observed
	# 	O11 = geneAB_samples
	# 	O12 = geneA_samples - geneAB_samples
	# 	O21 = geneB_samples - geneAB_samples
	# 	O22 = total_samples - geneA_samples - geneB_samples + geneAB_samples
	# 	obs = [O11, O12, O21, O22]
		
	# 	# expected
	# 	E11 = (geneA_samples) * (geneB_samples) * (m_multi_chromosome)
	# 	E12 = geneA_samples * (total_samples - geneB_samples)
	# 	E21 = geneB_samples * (total_samples - geneA_samples)
	# 	E22 = (total_samples - geneA_samples) * (total_samples - geneB_samples)
	# 	exp = [E11, E12, E21, E22] 

	# 	# apply Haldane correction if any observed category count is zero
	# 	if 0 in obs:
	# 		obs = [o + 0.5 for o in obs]
	# 		exp = [e + 0.5 for e in exp]

	# 	total_obs = sum(obs)
	# 	total_exp = sum(exp)
	# 	obs_freq = [o / total_obs for o in obs]
	# 	exp_freq = [e / total_exp for e in exp]

	# 	test_statistic = sum([(o-e)*(o-e)/e for o,e in zip(obs_freq, exp_freq)])
	# 	cdf = chi2.cdf(test_statistic, df=1)
	# 	p_val_two_sided = 1-cdf
	# 	diagonal_residual_sum = ((obs_freq[0]-exp_freq[0]) + (obs_freq[3]-exp_freq[3]))

	# 	odds_ratio = obs_freq[0] / exp_freq[0]
	# 	if odds_ratio > 1e9:
	# 		odds_ratio = 1e9 # cap ultra large to prevent infinite odds ratios

	# 	# convert to one-sided p-value
	# 	if diagonal_residual_sum >= 0:
	# 		p_val_one_sided = p_val_two_sided / 2
	# 	else:
	# 		p_val_one_sided = 1 - (p_val_two_sided / 2)

	# 	return p_val_one_sided, odds_ratio, d

	# def test_distance(self, a, b):
	# 	"""
	# 	temporary - just checking p-val speedup
	# 	"""
	# 	a_refs = self.nodes[self.gene_index[a]]['reference_genomes']
	# 	b_refs = self.nodes[self.gene_index[b]]['reference_genomes']
	# 	common_refs = set(a_refs).intersection(b_refs)
	# 	if not common_refs:
	# 		return -1
	# 	for ref in common_refs:
	# 		if a not in self.locs_by_genome[ref] or b not in self.locs_by_genome[ref]:
	# 			continue
	# 		a_info = self.locs_by_genome[ref][a]
	# 		b_info = self.locs_by_genome[ref][b]
	# 		if a_info[0] != b_info[0]:
	# 			return -1
	# 		else:
	# 			# Same chromosome, calculate distance
	# 			e_s = abs(a_info[2] - b_info[1])
	# 			s_e = abs(a_info[1] - b_info[2])
	# 			return min(e_s, s_e)
	# 	return -1

	# def Distance(self, a, b):
	# 	"""
    #     Calculate the distance between two genes, considering their reference genomes
    #     """
	# 	# Cache node lookups to avoid repeated DataFrame searches
	# 	if not hasattr(self, '_node_cache'):
	# 		self._node_cache = {}

	# 	# Get node indices from cache or compute
	# 	if a not in self._node_cache:
	# 		a_idx_list = self.nodes_df[self.nodes_df['label'] == a].index.tolist()
	# 		self._node_cache[a] = a_idx_list[0] if a_idx_list else None

	# 	if b not in self._node_cache:
	# 		b_idx_list = self.nodes_df[self.nodes_df['label'] == b].index.tolist()
	# 		self._node_cache[b] = b_idx_list[0] if b_idx_list else None

	# 	a_idx = self._node_cache[a]
	# 	b_idx = self._node_cache[b]

	# 	if a_idx is None or b_idx is None:
	# 		return -1  # One or both genes not found

	# 	# Get the reference genomes
	# 	a_refs = self.nodes_df.iloc[a_idx]['reference_genomes']
	# 	b_refs = self.nodes_df.iloc[b_idx]['reference_genomes']

	# 	# Find common reference genomes (faster with set intersection)
	# 	common_refs = set(a_refs).intersection(b_refs)

	# 	if not common_refs:
	# 		# Log the warning only in debug mode to avoid excessive output
	# 		return -1

	# 	# For each common reference, try to calculate distance
	# 	for ref in common_refs:
	# 		# Skip if gene coordinates aren't available
	# 		if a not in self.locs_by_genome[ref] or b not in self.locs_by_genome[ref]:
	# 			continue

	# 		# Check if they're on the same chromosome
	# 		if self.locs_by_genome[ref][a][0] != self.locs_by_genome[ref][b][0]:
	# 			continue  # Different chromosomes, try next reference

	# 		# Same chromosome, calculate distance
	# 		e_s = abs(self.locs_by_genome[ref][a][2] - self.locs_by_genome[ref][b][1])
	# 		s_e = abs(self.locs_by_genome[ref][a][1] - self.locs_by_genome[ref][b][2])
	# 		return min(e_s, s_e)

	# 	# If we get here, genes don't have coordinates in any common reference genome
	# 	return -1

	# def PVal(self, gene_a, gene_b, model='gamma_random_breakage'):
	# 	"""
    #     Generate p_val based on naive random breakage model, considering distance
    #     but not number of incidents of co-amplification

    #     Parameters:
    #         gene_a (str) : First gene label
    #         gene_b (str) : Second gene label
    #         model (str) : Statistical model to use
    #     Return:
    #         float or str : p-value or 'N/A' if distance couldn't be calculated
    #     """
	# 	# d = self.Distance(gene_a, gene_b)
	# 	d = self.test_distance(gene_a, gene_b)
	# 	if d == -1: return -1, d
	# 	params = models[model]
	# 	cdf = gamma.cdf(d, a=params[0], loc=params[1], scale=params[2])
	# 	return 1 - cdf, d

	# # note: need to reimplement if given an input list of p_vals from different tests
	# def QVal(self, p_values, alpha=0.05):
	# 	# extract valid p-values
	# 	valid_mask = [p != -1 for p in p_values]
	# 	valid_p_values = [p for p in p_values if p != -1]
	# 	# apply FDR correction only to valid p-values
	# 	_, valid_q_values = fdrcorrection(valid_p_values, alpha=alpha)
	# 	# reconstruct q_values with 'N/A' in the appropriate positions
	# 	valid_q_iter = iter(valid_q_values)
	# 	q_values = [next(valid_q_iter) if valid else -1 for valid in valid_mask]

	# 	return q_values
	# 	# q_values = ['N/A'] * len(p_values)
	# 	# j = 0
	# 	# for i, valid in enumerate(valid_mask):
	# 	# 	if valid:
	# 	# 		q_values[i] = valid_q_values[j]
	# 	# 		j += 1
	
	# # --------------------------------------------------------------------------


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

