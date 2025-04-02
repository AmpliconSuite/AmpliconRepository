import sys
import os

# a one-time usage script to convert a list of .fai files to a python dictionary of lengths

def get_chrom_lens(fai_file):
    chrom_len_dict = {}

    with open(fai_file) as infile:
        for line in infile:
            fields = line.rstrip().rsplit()
            if fields:
                if fields[0].startswith('chr'):
                    chrom_len_dict[fields[0].lstrip('chr')] = int(fields[1])
                else:
                    chrom_len_dict[fields[0]] = int(fields[1])

    return chrom_len_dict


fai_list = sys.argv[1:]
fai_len_dict = {}
for fai_file in fai_list:
    ref = os.path.basename(fai_file).rsplit("_noAlt")[0]
    print(ref, fai_file)
    chrom_lens = get_chrom_lens(fai_file)
    fai_len_dict[ref] = chrom_lens

print(fai_len_dict)
