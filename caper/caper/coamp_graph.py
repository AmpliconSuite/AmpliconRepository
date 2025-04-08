import re
import pandas as pd
import numpy as np
from collections import defaultdict
import time
import os
from scipy.stats import expon
from scipy.stats import gamma
from scipy.stats import chi2
from statsmodels.stats.multitest import fdrcorrection

models = {
	'gamma_random_breakage': [0.3987543483729932, # a
			1.999999999999, # loc
			1749495.5696758535 # scale
			]
}

class Graph:

	def __init__(self, dataset=None, col_format=None, amp_type="ecDNA", loc_type="feature"):
		"""
        Parameters:
            self (Graph) : Graph object
            dataset (tsv file) : AA aggregated results file
            amp_type (str) : type of focal amplification (ecDNA, BFB, etc.)
            loc_type (str) : cell line or feature
        Return:
            None
        """
		if dataset is None:
			print("Error: provide Amplicon Architect annotations")
		else:
			self.amp_type = amp_type
			self.loc_type = loc_type
			self.nodes = []
			self.edges = []
			self.total_samples = 0

			# Initialize dictionaries for each reference genome
			self.locs_by_genome = {}

			# Define reference genomes to process
			ref_genomes = ['GRCh38', 'GRCh37', 'hg19', "GRCh38_viral"]

			# Load gene location data for all possible reference genomes
			# This could be parallelized with multiprocessing if performance is critical
			import time
			start_time = time.time()
			for ref_genome in ref_genomes:
				start_ref = time.time()
				self.locs_by_genome[ref_genome] = self.ImportLocs(self.get_gene_bed_path(ref_genome))
				print(f"Loaded {ref_genome} gene locations in {time.time() - start_ref:.2f} seconds")
			print(f"Total gene location loading time: {time.time() - start_time:.2f} seconds")

			# Create nodes and edges from the combined dataset
			self.CreateNodes(dataset, col_format)
			self.CreateEdges()

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

	def CreateNodes(self, dataset, col_format):
		"""
        Create nodes while tracking the reference genome for each gene
        """
		start_time = time.time()
		print(f"Starting CreateNodes with {len(dataset)} rows")

		ALL_GENES = 'All genes' if col_format == 'space' else 'All_genes'
		FEATURE_ID = 'Feature ID' if col_format == 'space' else 'Feature_ID'
		REF_VERSION = 'Reference_version'  # Column name for reference genome

		# dictionary for fast lookups of gene labels
		gene_index = {}

		# get subset of ecDNA features
		filter_start = time.time()
		if self.loc_type == "feature":
			all_features = dataset[dataset['Classification'] == self.amp_type]
		else:
			all_features = dataset
		filter_end = time.time()
		print(
			f"Filtering features took {filter_end - filter_start:.4f} seconds, resulting in {len(all_features)} features")

		# for each feature
		process_start = time.time()
		gene_count = 0

		for genome in self.locs_by_genome:
			print(len(self.locs_by_genome[genome]))

		self.total_samples = len(all_features)

		for __, row in all_features.iterrows():
			# get the reference genome for this feature
			ref_genome = row.get(REF_VERSION, 'GRCh38')  # Default to GRCh38 if not specified

			# get the genes in this feature
			oncogenes = set(self.ExtractGenes(str(row['Oncogenes'])))
			all_genes = self.ExtractGenes(str(row[ALL_GENES]))
			gene_count += len(all_genes)

			for gene in all_genes:
				feature = row[FEATURE_ID]
				sample = feature.split("_amplicon")[0]

				# if the gene has been seen, add feature and cell line to info
				if gene in gene_index:
					index = gene_index[gene]
					self.nodes[index]['features'].append(feature)
					self.nodes[index]['samples'].append(sample)

					# Add this reference genome to the gene's reference list if not already there
					if ref_genome not in self.nodes[index]['reference_genomes']:
						self.nodes[index]['reference_genomes'].append(ref_genome)

				# otherwise add the gene to self.nodes as a new row
				else:
					if (gene in self.locs_by_genome[ref_genome]):
						location = f"chr{self.locs_by_genome[ref_genome][gene][0]}:{self.locs_by_genome[ref_genome][gene][1]}-{self.locs_by_genome[ref_genome][gene][2]}"
					else:
						location = 'N/A'

					node_info = {
						'label': gene,
						'oncogene': str(gene in oncogenes),
						'features': [feature],
						'samples': [sample],
						'location': location,
						'reference_genomes': [ref_genome]  # Track which reference genome(s) this gene appears in
					}
					self.nodes.append(node_info)
					gene_index[gene] = len(self.nodes) - 1
		process_end = time.time()
		print(
			f"Processing {gene_count} genes took {process_end - process_start:.4f} seconds, resulting in {len(self.nodes)} unique nodes")

		# remove potential duplicate features or cell lines
		dedup_start = time.time()
		for node_info in self.nodes:
			node_info['features'] = list(set(node_info['features']))
			node_info['samples'] = list(set(node_info['samples']))
		dedup_end = time.time()
		print(f"Deduplicating features and samples took {dedup_end - dedup_start:.4f} seconds")

		# concatenate all nodes as rows in df
		df_start = time.time()
		self.nodes_df = pd.DataFrame(self.nodes)
		df_end = time.time()
		print(f"Creating DataFrame took {df_end - df_start:.4f} seconds")

		total_time = time.time() - start_time
		print(f"Total CreateNodes execution: {total_time:.4f} seconds")

	def CreateEdges(self):
		"""
        Create edges, ensuring genes being connected share a reference genome
        """
		start_time = time.time()
		print(f"Starting CreateEdges with {len(self.nodes_df)} nodes")

		# extract relevant columns
		extract_start = time.time()
		labels = self.nodes_df['label'].values
		features = self.nodes_df['features'].values
		references = self.nodes_df['reference_genomes'].values
		extract_end = time.time()
		print(f"Extracting columns took {extract_end - extract_start:.4f} seconds")

		# build reverse index to map features to nodes
		index_start = time.time()
		feature_to_nodes = defaultdict(list)
		# Pre-compute feature to node mapping in bulk
		feature_node_pairs = [(amp, index)
							  for index, feature_list in enumerate(features)
							  for amp in feature_list]

		# Build mapping in one pass
		for amp, index in feature_node_pairs:
			feature_to_nodes[amp].append(index)
		index_end = time.time()
		print(
			f"Building feature index took {index_end - index_start:.4f} seconds with {len(feature_to_nodes)} unique features")

		# collect potential node pairs, avoiding duplicates
		pairs_start = time.time()
		potential_pairs = set()
		for nodelist in feature_to_nodes.values():
			for i, node1 in enumerate(nodelist):
				for node2 in nodelist[i + 1:]:
					potential_pairs.add((node1, node2))
		pairs_end = time.time()
		print(
			f"Collecting potential pairs took {pairs_end - pairs_start:.4f} seconds, found {len(potential_pairs)} pairs")

		# Early return if no pairs
		if not potential_pairs:
			self.edges = []
			self.edges_df = pd.DataFrame(self.edges)
			print("No potential pairs found, returning early")
			return

		# unzip pairs into source and target nodes
		pairs = list(potential_pairs)
		src_indices, tgt_indices = zip(*pairs)

		# validate pairs share a reference genome
		validate_start = time.time()
		valid_pairs = []
		valid_src = []
		valid_tgt = []

		for s, t in zip(src_indices, tgt_indices):
			src_refs = references[s]
			tgt_refs = references[t]

			# Check if they share at least one reference genome
			if any(ref in tgt_refs for ref in src_refs):
				valid_pairs.append((s, t))
				valid_src.append(s)
				valid_tgt.append(t)
		validate_end = time.time()
		print(
			f"Validating reference genomes took {validate_end - validate_start:.4f} seconds, {len(valid_pairs)} valid pairs")

		# Continue with only valid pairs
		src_indices = valid_src
		tgt_indices = valid_tgt

		# Early return if no valid pairs
		if not valid_src:
			self.edges = []
			self.edges_df = pd.DataFrame(self.edges)
			print("No valid pairs found, returning early")
			return

		# retrieve the matching features for all source and target nodes
		feature_start = time.time()
		src_features = [features[i] for i in src_indices]
		tgt_features = [features[j] for j in tgt_indices]

		# remove id information from feature labels
		clean_src_features = [[f.split("_amplicon")[0] for f in feature_list] for feature_list in src_features]
		clean_tgt_features = [[f.split("_amplicon")[0] for f in feature_list] for feature_list in tgt_features]
		feature_end = time.time()
		print(f"Processing feature lists took {feature_end - feature_start:.4f} seconds")

		# calculate all intersections and unions
		set_start = time.time()
		src_sets = [set(s) for s in clean_src_features]
		tgt_sets = [set(t) for t in clean_tgt_features]
		inters = [list(s & t) for s, t in zip(src_sets, tgt_sets)]
		unions = [list(s | t) for s, t in zip(src_sets, tgt_sets)]
		set_end = time.time()
		print(f"Set operations took {set_end - set_start:.4f} seconds")

		# filter pairs with non-empty intersections
		filter_start = time.time()
		non_empty_mask = [len(inter) > 0 for inter in inters]
		src_filtered = [src_indices[i] for i, mask in enumerate(non_empty_mask) if mask]
		tgt_filtered = [tgt_indices[i] for i, mask in enumerate(non_empty_mask) if mask]
		inters_filtered = [inters[i] for i, mask in enumerate(non_empty_mask) if mask]
		unions_filtered = [unions[i] for i, mask in enumerate(non_empty_mask) if mask]
		src_sets_filtered = [src_sets[i] for i, mask in enumerate(non_empty_mask) if mask]
		tgt_sets_filtered = [tgt_sets[i] for i, mask in enumerate(non_empty_mask) if mask]
		filter_end = time.time()
		print(
			f"Filtering non-empty intersections took {filter_end - filter_start:.4f} seconds, {len(src_filtered)} pairs remain")

		# Early return if no filtered pairs
		if not src_filtered:
			self.edges = []
			self.edges_df = pd.DataFrame(self.edges)
			print("No filtered pairs found, returning early")
			return

		# calculate weights
		weight_start = time.time()
		weights = [len(i) / len(u) for i, u in zip(inters_filtered, unions_filtered)]
		weight_end = time.time()
		print(f"Calculating weights took {weight_end - weight_start:.4f} seconds")

		# compute p and q values
		pval_start = time.time()
		chi_squared_results = [self.chi_squared_dep_test(labels[i], labels[j], src_sets_filtered[idx], tgt_sets_filtered[idx], inters_filtered[idx], self.total_samples) for idx, (i, j) in enumerate(zip(src_filtered, tgt_filtered))]
		p_values, odds_ratio, distances = zip(*chi_squared_results)
		p_values = list(p_values)
		odds_ratio = list(odds_ratio)
		distances = list(distances)
		pval_end = time.time()
		print(f"Calculating p-values took {pval_end - pval_start:.4f} seconds")

		qval_start = time.time()
		q_values = self.QVal(p_values)
		qval_end = time.time()
		print(f"Calculating q-values took {qval_end - qval_start:.4f} seconds")

		# create edges dict and df
		edges_start = time.time()
		self.edges = [
			{
				'source': labels[i],
				'target': labels[j],
				'weight': weights[idx],
				'inter': inters_filtered[idx],
				'union': unions_filtered[idx],
				'distance': distances[idx], 
				'pval': p_values[idx],
				'qval': q_values[idx],
				'odds_ratio': odds_ratio[idx]
			}
			for idx, (i, j) in enumerate(zip(src_filtered, tgt_filtered))
		]
		self.edges_df = pd.DataFrame(self.edges)
		edges_end = time.time()
		print(f"Creating edges DataFrame took {edges_end - edges_start:.4f} seconds, {len(self.edges)} edges created")

		total_time = time.time() - start_time
		print(f"Total CreateEdges execution: {total_time:.4f} seconds")

	# helper functions
	# ----------------
	def ExtractGenes(self, input):
		"""
		Parameters: 
			input (str) : ['A', 'B', 'C'] or ["'A'", "'B'", "'C'"]
		Return: 
			list: ["A", "B", "C"]
		"""
		pattern = r"['\"]?([\w./-]+)['\"]?"
		genelist = re.findall(pattern, input)
		return genelist

	def ImportLocs(self, bed_file):
		"""
        Return a dict of format: {gene (string): (start chromosome (string),
        start coordinate (int), end coordinate (int))}
        """
		locs = {}
		try:
			gene_coords = pd.read_csv(bed_file, sep="\t", header=None)
			# Vectorized operation for chromosome naming
			gene_coords['chr_num'] = gene_coords[0].str[-1]
			gene_coords.loc[~gene_coords['chr_num'].str.match(r'[0-9XY]'), 'chr_num'] = gene_coords[0]

			# Convert to dictionary in one operation instead of row-by-row
			locs = dict(zip(gene_coords[3],
							zip(gene_coords['chr_num'],
								gene_coords[1].astype(int),
								gene_coords[2].astype(int))))
		except Exception as e:
			print(f"Error importing gene coordinates from {bed_file}: {e}")

		return locs

	def Distance(self, a, b):
		"""
        Calculate the distance between two genes, considering their reference genomes
        """
		# Cache node lookups to avoid repeated DataFrame searches
		if not hasattr(self, '_node_cache'):
			self._node_cache = {}

		# Get node indices from cache or compute
		if a not in self._node_cache:
			a_idx_list = self.nodes_df[self.nodes_df['label'] == a].index.tolist()
			self._node_cache[a] = a_idx_list[0] if a_idx_list else None

		if b not in self._node_cache:
			b_idx_list = self.nodes_df[self.nodes_df['label'] == b].index.tolist()
			self._node_cache[b] = b_idx_list[0] if b_idx_list else None

		a_idx = self._node_cache[a]
		b_idx = self._node_cache[b]

		if a_idx is None or b_idx is None:
			return -1  # One or both genes not found

		# Get the reference genomes
		a_refs = self.nodes_df.iloc[a_idx]['reference_genomes']
		b_refs = self.nodes_df.iloc[b_idx]['reference_genomes']

		# Find common reference genomes (faster with set intersection)
		common_refs = set(a_refs).intersection(b_refs)

		if not common_refs:
			# Log the warning only in debug mode to avoid excessive output
			return -1

		# For each common reference, try to calculate distance
		for ref in common_refs:
			# Skip if gene coordinates aren't available
			if a not in self.locs_by_genome[ref] or b not in self.locs_by_genome[ref]:
				continue

			# Check if they're on the same chromosome
			if self.locs_by_genome[ref][a][0] != self.locs_by_genome[ref][b][0]:
				continue  # Different chromosomes, try next reference

			# Same chromosome, calculate distance
			return abs(self.locs_by_genome[ref][a][1] - self.locs_by_genome[ref][b][1])

		# If we get here, genes don't have coordinates in any common reference genome
		return -1

	def PVal(self, gene_a, gene_b, model='gamma_random_breakage'):
		"""
        Generate p_val based on naive random breakage model, considering distance
        but not number of incidents of co-amplification

        Parameters:
            gene_a (str) : First gene label
            gene_b (str) : Second gene label
            model (str) : Statistical model to use
        Return:
            float or str : p-value or 'N/A' if distance couldn't be calculated
        """
		d = self.Distance(gene_a, gene_b)
		if d == -1: return -1, d
		params = models[model]
		cdf = gamma.cdf(d, a=params[0], loc=params[1], scale=params[2])
		return 1 - cdf, d

	def chi_squared_dep_test(self, gene_a, gene_b, src_sets_filtered, tgt_sets_filtered, inters_filtered, total_samples):
		pdD, d = self.PVal(gene_a, gene_b)
		if pdD == -1: return -1, -1, -1
			
		geneA_samples = len(src_sets_filtered)
		geneB_samples = len(tgt_sets_filtered)
		geneAB_samples = len(inters_filtered)
		fAandB = geneAB_samples/total_samples
		fA = geneA_samples/total_samples
		fB = geneB_samples/total_samples
		fAorB = fA + fB - fAandB

		# observed
		O11 = geneAB_samples
		O12 = geneA_samples - geneAB_samples
		O21 = geneB_samples - geneAB_samples
		O22 = total_samples - geneA_samples - geneB_samples + geneAB_samples
		obs = [O11, O12, O21, O22]
		
		# expected
		E11 = (geneA_samples + geneB_samples - geneAB_samples) * pdD
		E12 = geneA_samples * (1 - pdD)
		E21 = geneB_samples * (1 - pdD)
		E22 = total_samples - (geneA_samples + geneB_samples - geneAB_samples)
		exp = [E11, E12, E21, E22] 

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

		# Convert to one-sided p-value
		if diagonal_residual_sum >= 0:
			p_val_one_sided = p_val_two_sided / 2
		else:
			p_val_one_sided = 1 - (p_val_two_sided / 2)

		return p_val_one_sided, odds_ratio, d
	
	def QVal(self, p_values, alpha=0.05):
		# extract valid p-values
		valid_mask = [p != -1 for p in p_values]
		valid_p_values = [p for p in p_values if p != -1]
		# apply FDR correction only to valid p-values
		_, valid_q_values = fdrcorrection(valid_p_values, alpha=alpha)
		# reconstruct q_values with 'N/A' in the appropriate positions
		valid_q_iter = iter(valid_q_values)
		q_values = [next(valid_q_iter) if valid else -1 for valid in valid_mask]

		return q_values
		# q_values = ['N/A'] * len(p_values)
		# j = 0
		# for i, valid in enumerate(valid_mask):
		# 	if valid:
		# 		q_values[i] = valid_q_values[j]
		# 		j += 1

	# get functions
	# -------------
	def Locs(self):
		return self.locs
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

