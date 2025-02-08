import re
import pandas as pd
import numpy as np
from collections import defaultdict
import time

# attempt to streamline by creating node and edge dataframes directly
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

			self.CreateNodes(dataset)
			self.CreateEdges()

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
				cell_line = feature.split("_")[0]
				# if the gene has been seen, add feature and cell line to info
				if gene in gene_index:
					index = gene_index[gene]
					self.nodes[index]['features'].append(feature)
					self.nodes[index]['cell_lines'].append(cell_line)
				# otherwise add the gene to self.nodes as a new row
				else:
					node_info = {
						'label': gene,
						'oncogene': str(gene in oncogenes),
						'features': [feature],
						'cell_lines': [cell_line]
                	}
					self.nodes.append(node_info)
					gene_index[gene] = len(self.nodes) - 1
    	
		# remove potential duplicate features or cell lines
		for node_info in self.nodes:
			node_info['features'] = list(set(node_info['features']))
			node_info['cell_lines'] = list(set(node_info['cell_lines']))
	
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
		start = time.process_time()

		# create edges dict and df
		self.edges = [
			{
				'source': labels[i],
				'target': labels[j],
				'weight': weights[idx],
				'inter': inters_filtered[idx],
				'union': unions_filtered[idx]
			}
			for idx, (i, j) in enumerate(zip(src_filtered, tgt_filtered))
		]
		self.edges_df = pd.DataFrame(self.edges)

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
