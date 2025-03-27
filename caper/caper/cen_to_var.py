import sys
import os
import copy

# a one-time usage script to convert the centromere bed files to a python dictionary.

def get_chrom_num(location: str):
    location = location.replace("'", "") # clean any apostrophes out
    raw_contig_id = location.rsplit(":")[0]
    if raw_contig_id.startswith('chr'):
        return raw_contig_id[3:]

    return raw_contig_id


cent_file_list = sys.argv[1:]

ref_to_cent_dict = {}
for cent_file in cent_file_list:
    ref = os.path.basename(cent_file).rsplit("_centromere")[0]
    print(ref, cent_file)

    full_cent_dict = {}
    with open(cent_file) as infile:
        for line in infile:
            fields = line.rsplit("\t")
            chr_num = get_chrom_num(fields[0])
            s, e = int(fields[1]), int(fields[2])
            if chr_num not in full_cent_dict:
                full_cent_dict[chr_num] = (s, e)
            else:
                cp = full_cent_dict[chr_num]
                full_cent_dict[chr_num] = (min(cp[0], s), max(cp[1], e))

    ref_to_cent_dict[ref] = copy.deepcopy(full_cent_dict)

print(ref_to_cent_dict)
