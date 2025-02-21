import re
import pandas as pd
import numpy as np
from collections import defaultdict
import time
import os
from scipy.stats import expon
from scipy.stats import gamma
from statsmodels.stats.multitest import fdrcorrection

models = {
	'gamma_random_breakage': [0.3987543483729932, # a
			1.999999999999, # loc
			1749495.5696758535 # scale
			]
}

class Graph:

	def __init__(self, dataset=None, amp_type="ecDNA", loc_type="feature"):
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
			self.locs = self.ImportLocs(os.environ['CAPER_ROOT'] + '/caper/bed_files/hg38_genes.bed')

			self.CreateNodes(dataset)
			self.CreateEdges()

	def CreateNodes(self, dataset):
		"""
		# OUTDATED DESC
		Create a nodes_df by iterating through the dataset, adding new genes to 
		a list of dictionaries. Update the amplicons for existing genes, then 
		merge the list into the dataframe after processing.
				
		Parameters: 
			self (Graph) : Graph object 
			dataset (tsv file) : AA aggregated results file
		Return: 
			None
		"""
		# dictionary for fast lookups of gene labels
		gene_index = {}

		# get subset of ecDNA features
		if self.loc_type == "feature":
			all_features = dataset[dataset['Classification'] == self.amp_type]
		# for each feature
		for __, row in all_features.iterrows():
			# get the genes in this feature
			oncogenes = set(self.ExtractGenes(str(row['Oncogenes'])))
			all_genes = self.ExtractGenes(str(row['All_genes']))
			for gene in all_genes:
				feature = row['Feature_ID']
				sample = feature.split("_amplicon")[0]
				# if the gene has been seen, add feature and cell line to info
				if gene in gene_index:
					index = gene_index[gene]
					self.nodes[index]['features'].append(feature)
					self.nodes[index]['samples'].append(sample)
				# otherwise add the gene to self.nodes as a new row
				else:
					node_info = {
						'label': gene,
						'oncogene': str(gene in oncogenes),
						'features': [feature],
						'samples': [sample],
						# 'chromosome': self.locs[gene][0],
						# 'start_coord': self.locs[gene][1],
						# 'end_coord': self.locs[gene][2]
                	}
					self.nodes.append(node_info)
					gene_index[gene] = len(self.nodes) - 1
    	
		# remove potential duplicate features or cell lines
		for node_info in self.nodes:
			node_info['features'] = list(set(node_info['features']))
			node_info['samples'] = list(set(node_info['samples']))
	
		# concatenate all nodes as rows in df
		self.nodes_df = pd.DataFrame(self.nodes)
			
	def CreateEdges(self):
		"""
		# OUTDATED DESC
		Create edges by iterating through pairs of nodes in nodes_df based on 
		shared features. Calculate edge properties for each pair, and append 
		the results to the edges_df after processing all pairs.
				
		Parameters: 
			self (Graph) : Graph object 
		Return: 
			None
		"""
		# extract relevant columns
		labels = self.nodes_df['label'].values
		features = self.nodes_df['features'].values
		
		# build reverse index to map features to nodes
		feature_to_nodes = defaultdict(list)
		for index, amps in enumerate(features):
			for amp in amps:
				feature_to_nodes[amp].append(index)

		# collect potential node pairs, avoiding duplicates
		potential_pairs = set()
		for nodelist in feature_to_nodes.values():
			for i, node1 in enumerate(nodelist):
				for node2 in nodelist[i + 1:]:
					potential_pairs.add((node1, node2)) 

		# unzip pairs into source and target nodes
		pairs = list(potential_pairs)
		src_indices, tgt_indices = zip(*pairs)
		
		# retrieve the matching features for all source and target nodes
		src_features = [features[i] for i in src_indices]
		tgt_features = [features[j] for j in tgt_indices]
		
		# remove id information from feature labels
		for feature_list in src_features:
			feature_list = [f.split("_amplicon")[0] for f in feature_list]
		for feature_list in tgt_features:
			feature_list = [f.split("_amplicon")[0] for f in feature_list]

		# calculate all intersections and unions
		inters = [list(set(s) & set(t)) for s,t in zip(src_features, tgt_features)]
		unions = [list(set(s) | set(t)) for s,t in zip(src_features, tgt_features)]

		# filter pairs with non-empty intersections
		non_empty_mask = [len(inter) > 0 for inter in inters]
		src_filtered = [src_indices[i] for i, mask in enumerate(non_empty_mask) if mask]
		tgt_filtered = [tgt_indices[i] for i, mask in enumerate(non_empty_mask) if mask]
		inters_filtered = [inters[i] for i, mask in enumerate(non_empty_mask) if mask]
		unions_filtered = [unions[i] for i, mask in enumerate(non_empty_mask) if mask]

		# calculate weights
		weights = [len(i) / len(u) for i,u in zip(inters_filtered, unions_filtered)]

		# compute p and q values
		p_values = [self.PVal(self.Distance(a, b)) for a,b in zip(src_filtered, tgt_filtered)]
		q_values = self.QVal(p_values)

		# create edges dict and df
		self.edges = [
			{
				'source': labels[i],
				'target': labels[j],
				'weight': weights[idx],
				'inter': inters_filtered[idx],
				'union': unions_filtered[idx],
				'pval': p_values[idx],
				'qval': q_values[idx]
			}
			for idx, (i, j) in enumerate(zip(src_filtered, tgt_filtered))
		]
		self.edges_df = pd.DataFrame(self.edges)

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
		return a dict of format: {gene (string): (start chromosome (string), 
		start coordinate (int))}
		"""
		gene_coords = pd.read_csv(bed_file, sep="\t", header=None)
		gene_coords['chr_num'] = gene_coords.apply(lambda row: row[0][-1], axis=1)
		locs = {}
		for i,row in gene_coords.iterrows():
			locs[row[3]] = (row['chr_num'], int(row[1]), int(row[2]))
		return locs
	
	def Distance(self, a, b):
		# multi chr
		if self.locs[a][0] != self.locs[b][0]:
			return 'N/A'
		# single chr
		return abs(self.locs[a][1] - self.locs[b][1])

	def PVal(d, model='gamma_random_breakage'):
		"""
		generate p_val based on naive random breakage model, considering distance
		but not number of incidents of co-amplification
		"""
		if d == 'N/A': return d
		params = models[model]
		cdf = gamma.cdf(d, a=params[0], loc=params[1], scale=params[2])
		return 1-cdf
		# if model == 'expon':
		# 	d = distance(a, b)
		# 	if not type(d) is str:
		# 		cdf = expon.cdf(d, loc=params[0], scale=params[1])
		# 		return 1-cdf
	
	def QVal(p_values, alpha=0.05):
		# extract valid p-values
		valid_mask = [p != 'N/A' for p in p_values]
		valid_p_values = [p for p in p_values if p != 'N/A']

		# apply FDR correction only to valid p-values
		_, valid_q_values = fdrcorrection(valid_p_values, alpha=alpha)

		# reconstruct q_values with 'N/A' in the appropriate positions
		valid_q_iter = iter(valid_q_values)
		q_values = [next(valid_q_iter) if valid else 'N/A' for valid in valid_mask]

		return q_values
		# q_values = ['N/A'] * len(p_values)
		# j = 0
		# for i, valid in enumerate(valid_mask):
		# 	if valid:
		# 		q_values[i] = valid_q_values[j]
		# 		j += 1

	# get functions
	# -------------
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

