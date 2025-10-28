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

