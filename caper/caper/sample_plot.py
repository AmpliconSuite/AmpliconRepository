import logging
import pandas as pd
import plotly.graph_objs as go
import numpy as np
import warnings
from plotly.subplots import make_subplots
from pylab import cm
import os
from .utils import get_db_handle, get_collection_handle
import gridfs
from bson.objectid import ObjectId
from io import StringIO
import time
from pandas.api.types import is_numeric_dtype

warnings.filterwarnings("ignore")

# FOR LOCAL DEVELOPMENT [Deprecated]
# db_handle, mongo_client = get_db_handle('caper', 'mongodb://localhost:27017')

# FOR PRODUICTION
# db_handle, mongo_client = get_db_handle('caper', os.environ['DB_URI_SECRET'])


ref_cent_dict = {'GRCh37': {'1': (121500000, 128900000), '10': (38000000, 42300000), '11': (51600000, 55700000), '12': (33300000, 38200000), '13': (16300000, 19500000), '14': (16100000, 19100000), '15': (15800000, 20700000), '16': (34600000, 38600000), '17': (22200000, 25800000), '18': (15400000, 19000000), '19': (24400000, 28600000), '2': (90500000, 96800000), '20': (25600000, 29400000), '21': (10900000, 14300000), '22': (12200000, 17900000), '3': (87900000, 93900000), '4': (48200000, 52700000), '5': (46100000, 50700000), '6': (58700000, 63300000), '7': (58000000, 61700000), '8': (43100000, 48100000), '9': (47300000, 50700000), 'X': (58100000, 63000000), 'Y': (11600000, 13400000)},
 'GRCh38': {'1': (121700000, 125100000), '10': (38000000, 41600000), '11': (51000000, 55800000), '12': (33200000, 37800000), '13': (16500000, 18900000), '14': (16100000, 18200000), '15': (17500000, 20500000), '16': (35300000, 38400000), '17': (22700000, 27400000), '18': (15400000, 21500000), '19': (24200000, 28100000), '2': (91800000, 96000000), '20': (25700000, 30400000), '21': (10900000, 13000000), '22': (13700000, 17400000), '3': (87800000, 94000000), '4': (48200000, 51800000), '5': (46100000, 51400000), '6': (58500000, 62600000), '7': (58100000, 62100000), '8': (43200000, 47200000), '9': (42200000, 45500000), 'X': (58100000, 63800000), 'Y': (10300000, 10600000)},
 'GRCh38_viral': {'1': (121700000, 125100000), '10': (38000000, 41600000), '11': (51000000, 55800000), '12': (33200000, 37800000), '13': (16500000, 18900000), '14': (16100000, 18200000), '15': (17500000, 20500000), '16': (35300000, 38400000), '17': (22700000, 27400000), '18': (15400000, 21500000), '19': (24200000, 28100000), '2': (91800000, 96000000), '20': (25700000, 30400000), '21': (10900000, 13000000), '22': (13700000, 17400000), '3': (87800000, 94000000), '4': (48200000, 51800000), '5': (46100000, 51400000), '6': (58500000, 62600000), '7': (58100000, 62100000), '8': (43200000, 47200000), '9': (42200000, 45500000), 'X': (58100000, 63800000), 'Y': (10300000, 10600000)},
 'hg19': {'1': (121500000, 128900000), '10': (38000000, 42300000), '11': (51600000, 55700000), '12': (33300000, 38200000), '13': (16300000, 19500000), '14': (16100000, 19100000), '15': (15800000, 20700000), '16': (34600000, 38600000), '17': (22200000, 25800000), '18': (15400000, 19000000), '19': (24400000, 28600000), '2': (90500000, 96800000), '20': (25600000, 29400000), '21': (10900000, 14300000), '22': (12200000, 17900000), '3': (87900000, 93900000), '4': (48200000, 52700000), '5': (46100000, 50700000), '6': (58700000, 63300000), '7': (58000000, 61700000), '8': (43100000, 48100000), '9': (47300000, 50700000), 'X': (58100000, 63000000), 'Y': (11600000, 13400000)},
 'mm10': {'1': (110000, 3000000), '10': (110000, 3000000), '11': (110000, 3000000), '12': (110000, 3000000), '13': (110000, 3000000), '14': (110000, 3000000), '15': (110000, 3000000), '16': (110000, 3000000), '17': (110000, 3000000), '18': (110000, 3000000), '19': (110000, 3000000), '2': (110000, 3000000), '3': (110000, 3000000), '4': (110000, 3000000), '5': (110000, 3000000), '6': (110000, 3000000), '7': (110000, 3000000), '8': (110000, 3000000), '9': (110000, 3000000), 'X': (110000, 3000000), 'Y': (4072168, 4161965)}}


ref_length_dict = {'GRCh37': {'1': 249250621, '2': 243199373, '3': 198022430, '4': 191154276, '5': 180915260, '6': 171115067, '7': 159138663, '8': 146364022, '9': 141213431, '10': 135534747, '11': 135006516, '12': 133851895, '13': 115169878, '14': 107349540, '15': 102531392, '16': 90354753, '17': 81195210, '18': 78077248, '19': 59128983, '20': 63025520, '21': 48129895, '22': 51304566, 'X': 155270560, 'Y': 59373566, 'MT': 16569},
'GRCh38': {'1': 248956422, '2': 242193529, '3': 198295559, '4': 190214555, '5': 181538259, '6': 170805979, '7': 159345973, '8': 145138636, '9': 138394717, '10': 133797422, '11': 135086622, '12': 133275309, '13': 114364328, '14': 107043718, '15': 101991189, '16': 90338345, '17': 83257441, '18': 80373285, '19': 58617616, '20': 64444167, '21': 46709983, '22': 50818468, 'X': 156040895, 'Y': 57227415, 'M': 16569},
'GRCh38_viral': {'aapv1ref_1': 8095, 'aspv1ref_1': 7589, 'bgpv1ref_1': 7946, 'bppv1ref_1': 7737, 'bpv10ref_1': 15183, 'bpv11ref_1': 7251, 'bpv12ref_1': 7197, 'bpv13ref_1': 7961, 'bpv14ref_1': 7966, 'bpv15ref_1': 7189, 'bpv1ref_1': 7946, 'bpv2ref_1': 7937, 'bpv3ref_1': 7276, 'bpv4ref_1': 7265, 'bpv5ref_1': 7840, 'bpv6ref_1': 7296, 'bpv7ref_1': 7412, 'bpv8ref_1': 7791, 'bpv9ref_1': 15045, '1': 248956422, '10': 133797422, '11': 135086622, '12': 133275309, '13': 114364328, '14': 107043718, '15': 101991189, '16': 90338345, '17': 83257441, '18': 80373285, '19': 58617616, '2': 242193529, '20': 64444167, '21': 46709983, '22': 50818468, '3': 198295559, '4': 190214555, '5': 181538259, '6': 170805979, '7': 159345973, '8': 145138636, '9': 138394717, 'M': 16569, 'X': 156040895, 'Y': 57227415, 'ddpv1ref_1': 7852, 'eapv1ref_1': 7467, 'ecpv1ref_1': 7610, 'ecpv2ref_1': 7803, 'ecpv3ref_1': 7582, 'ecpv4ref_1': 15898, 'ecpv5ref_1': 7519, 'ecpv6ref_1': 7551, 'ecpv7ref_1': 7619, 'edpv1ref_1': 7428, 'eepv1ref_1': 8256, 'ehelpv1ref_1': 7891, 'elpv1ref_1': 8194, 'eserpv1ref_1': 7668, 'eserpv2ref_1': 7574, 'eserpv3ref_1': 7711, 'fcapv1ref_1': 8300, 'fcapv2ref_1': 7899, 'fcapv3ref_1': 7583, 'fcapv4ref_1': 7616, 'fcpv1ref_1': 15505, 'fgpv1ref_1': 8132, 'flpv1ref_1': 7498, 'hpv100ref_1': 7380, 'hpv101ref_1': 7259, 'hpv102ref_1': 8078, 'hpv103ref_1': 7263, 'hpv104ref_1': 15418, 'hpv105ref_1': 15209, 'hpv106ref_1': 8035, 'hpv107ref_1': 7562, 'hpv108ref_1': 7150, 'hpv109ref_1': 7346, 'hpv10ref_1': 7919, 'hpv110ref_1': 7423, 'hpv111ref_1': 7386, 'hpv112ref_1': 7227, 'hpv113ref_1': 7412, 'hpv114ref_1': 8070, 'hpv115ref_1': 7476, 'hpv116ref_1': 7184, 'hpv117ref_1': 7895, 'hpv118ref_1': 7597, 'hpv119ref_1': 7251, 'hpv11ref_1': 7931, 'hpv120ref_1': 7304, 'hpv121ref_1': 7342, 'hpv122ref_1': 7397, 'hpv123ref_1': 7329, 'hpv124ref_1': 7489, 'hpv125ref_2': 7809, 'hpv126ref_1': 7326, 'hpv127ref_1': 7181, 'hpv128ref_1': 7259, 'hpv129ref_1': 7219, 'hpv12ref_1': 7673, 'hpv130ref_1': 7388, 'hpv131ref_1': 7182, 'hpv132ref_1': 7125, 'hpv133ref_1': 7358, 'hpv134ref_1': 7309, 'hpv135ref_1': 7293, 'hpv136ref_1': 7319, 'hpv137ref_1': 7236, 'hpv138ref_1': 7353, 'hpv139ref_1': 15419, 'hpv13ref_1': 7880, 'hpv140ref_1': 7341, 'hpv141ref_1': 7384, 'hpv142ref_1': 7374, 'hpv143ref_1': 7715, 'hpv144ref_1': 7271, 'hpv145ref_1': 15149, 'hpv146ref_1': 7265, 'hpv147ref_1': 7224, 'hpv148ref_1': 7164, 'hpv149ref_1': 7333, 'hpv14ref_1': 7713, 'hpv150ref_1': 7436, 'hpv151ref_1': 7386, 'hpv152ref_1': 7480, 'hpv153ref_1': 7240, 'hpv154ref_1': 7286, 'hpv155ref_1': 7352, 'hpv156ref_1': 15430, 'hpv159ref_1': 7443, 'hpv15ref_1': 7413, 'hpv160ref_1': 7779, 'hpv161ref_1': 7238, 'hpv162ref_1': 7214, 'hpv163ref_1': 7233, 'hpv164ref_1': 7233, 'hpv165ref_1': 7129, 'hpv166ref_1': 7212, 'hpv167ref_1': 7228, 'hpv168ref_1': 7204, 'hpv169ref_1': 7252, 'hpv16ref_1': 7906, 'hpv170ref_1': 7417, 'hpv171ref_1': 7261, 'hpv172ref_1': 7203, 'hpv173ref_1': 7297, 'hpv174ref_1': 7359, 'hpv175ref_1': 7226, 'hpv178ref_1': 7314, 'hpv179ref_1': 7228, 'hpv17ref_1': 7426, 'hpv180ref_1': 15042, 'hpv184ref_1': 7324, 'hpv18ref_1': 7857, 'hpv197ref_1': 7278, 'hpv199ref_1': 7184, 'hpv19ref_1': 14705, 'hpv1ref_1': 7816, 'hpv200ref_1': 7137, 'hpv201ref_1': 7291, 'hpv202ref_1': 7344, 'hpv204ref_1': 7227, 'hpv20ref_1': 7757, 'hpv21ref_1': 7779, 'hpv22ref_1': 7368, 'hpv23ref_1': 7324, 'hpv24ref_1': 7452, 'hpv25ref_1': 7713, 'hpv26ref_1': 7855, 'hpv27ref_1': 7823, 'hpv28ref_1': 7959, 'hpv29ref_1': 7916, 'hpv2ref_1': 7860, 'hpv30ref_1': 7852, 'hpv31ref_1': 7912, 'hpv32ref_1': 7961, 'hpv33ref_1': 7909, 'hpv34ref_1': 7723, 'hpv35ref_1': 7879, 'hpv36ref_1': 7722, 'hpv37ref_1': 7421, 'hpv38ref_1': 7400, 'hpv39ref_1': 7833, 'hpv3ref_1': 7820, 'hpv40ref_1': 7909, 'hpv41ref_1': 7614, 'hpv42ref_1': 7917, 'hpv43ref_1': 7975, 'hpv44ref_1': 7833, 'hpv45ref_1': 7858, 'hpv47ref_1': 7726, 'hpv48ref_1': 7100, 'hpv49ref_1': 7560, 'hpv4ref_1': 7353, 'hpv50ref_1': 7184, 'hpv51ref_1': 7808, 'hpv52ref_1': 7942, 'hpv53ref_1': 7859, 'hpv54ref_1': 7759, 'hpv56ref_1': 7845, 'hpv57ref_1': 15734, 'hpv58ref_1': 7824, 'hpv59ref_1': 7896, 'hpv5ref_1': 7746, 'hpv60ref_1': 7313, 'hpv61ref_1': 7989, 'hpv62ref_1': 8092, 'hpv63ref_1': 7348, 'hpv65ref_1': 7308, 'hpv66ref_1': 7824, 'hpv67ref_1': 7801, 'hpv68ref_1': 7822, 'hpv69ref_1': 7700, 'hpv6ref_1': 7996, 'hpv70ref_1': 7905, 'hpv71ref_1': 8037, 'hpv72ref_1': 7989, 'hpv73ref_1': 7700, 'hpv74ref_1': 7887, 'hpv75ref_1': 7537, 'hpv76ref_1': 7549, 'hpv77ref_1': 7887, 'hpv78ref_1': 23453, 'hpv7ref_1': 8027, 'hpv80ref_1': 7427, 'hpv81ref_1': 8070, 'hpv82ref_1': 7870, 'hpv83ref_1': 24953, 'hpv84ref_1': 7948, 'hpv85ref_1': 7812, 'hpv86ref_1': 15889, 'hpv87ref_2': 7999, 'hpv88ref_1': 7326, 'hpv89ref_1': 8078, 'hpv8ref_1': 7654, 'hpv90ref_1': 8033, 'hpv91ref_1': 7966, 'hpv92ref_1': 7461, 'hpv93ref_1': 7450, 'hpv94ref_1': 7881, 'hpv95ref_1': 7337, 'hpv96ref_2': 7438, 'hpv97ref_1': 7843, 'hpv98ref_2': 7466, 'hpv99ref_1': 7698, 'hpv9ref_1': 22677, 'hpv_mcg2nr_1': 7152, 'hpv_mcg3nr_1': 7095, 'hpv_mch2nr_1': 7190, 'hpv_mfa75_ki88_03nr_1': 7401, 'hpv_mfd1nr_1': 14239, 'hpv_mfd2nr_1': 7219, 'hpv_mfi864nr_2': 7247, 'hpv_mfs1nr_1': 7167, 'hpv_mkc5nr_1': 7143, 'hpv_mkn1nr_1': 7300, 'hpv_mkn2nr_1': 7299, 'hpv_mkn3nr_1': 7251, 'hpv_ml55nr_1': 7177, 'hpv_mrtrx7nr_1': 7731, 'hpv_msd2nr_1': 7300, 'hpv_mse355nr_1': 15308, 'hpv_mse379nr_1': 7221, 'hpv_mse383nr_1': 7317, 'hpv_mse435nr_1': 7291, 'lrpv1ref_1': 8233, 'mapv1ref_1': 7647, 'mcpv2ref_1': 7522, 'mfpv10ref_1': 7920, 'mfpv11ref_1': 8014, 'mfpv1ref_1': 7588, 'mfpv2ref_1': 7632, 'mfpv3ref_2': 7935, 'mfpv4ref_1': 7950, 'mfpv5ref_1': 7990, 'mfpv6ref_1': 7943, 'mfpv7ref_1': 8063, 'mfpv8ref_1': 8001, 'mfpv9ref_1': 7988, 'mmipv1ref_1': 7393, 'mmpv1ref_1': 8028, 'mmupv1ref_1': 7510, 'mnpv1ref_1': 7687, 'mppv1ref_1': 7985, 'mrpv1ref_1': 7339, 'mscpv1ref_1': 7632, 'mscpv2ref_1': 7531, 'mspv1ref_1': 7047, 'oapv1ref_1': 7761, 'oapv2ref_1': 15559, 'oapv3ref_1': 7344, 'ocpv1ref_1': 7565, 'ovpv1ref_1': 8374, 'papv1ref_1': 7637, 'pcpv1ref_1': 8321, 'pepv1ref_1': 7304, 'phpv1ref_1': 8008, 'plppv1ref_1': 15993, 'plpv1ref_1': 8170, 'pmpv1ref_1': 7704, 'pphpv1ref_1': 7596, 'pphpv2ref_1': 7635, 'pphpv4ref_1': 7348, 'pppv1ref_1': 7902, 'pspv1ref_1': 7879, 'psupv1ref_1': 7630, 'ralpv1ref_1': 15855, 'rapv1ref_1': 7970, 'rferpv1ref_1': 16204, 'rnpv1ref_1': 7378, 'rnpv2ref_1': 7724, 'rnpv3ref_1': 7707, 'rruppv1ref_1': 7256, 'rtpv1ref_1': 8090, 'rtpv2ref_1': 7267, 'sfpv1ref_1': 7868, 'sscpv1ref_1': 7596, 'sscpv2ref_1': 7657, 'sscpv3ref_1': 7664, 'sspv1ref_1': 7260, 'tepv1ref_1': 7497, 'tmpv1ref_1': 7722, 'tmpv2ref_1': 7855, 'tmpv3ref_1': 7622, 'tmpv4ref_2': 7771, 'ttpv1ref_1': 15768, 'ttpv2ref_1': 7866, 'ttpv3ref_1': 7915, 'ttpv4ref_1': 7792, 'ttpv5ref_1': 7853, 'ttpv6ref_1': 7895, 'ttpv7ref_1': 7783, 'umpv1ref_1': 7582, 'uupv1ref_1': 8078, 'vvpv1ref_1': 7519, 'zcpv1ref_1': 7584},
'hg19': {'1': 249250621, '2': 243199373, '3': 198022430, '4': 191154276, '5': 180915260, '6': 171115067, '7': 159138663, '8': 146364022, '9': 141213431, '10': 135534747, '11': 135006516, '12': 133851895, '13': 115169878, '14': 107349540, '15': 102531392, '16': 90354753, '17': 81195210, '18': 78077248, '19': 59128983, '20': 63025520, '21': 48129895, '22': 51304566, 'X': 155270560, 'Y': 59373566, 'M': 16571},
'mm10': {'1': 195471971, '10': 130694993, '11': 122082543, '12': 120129022, '13': 120421639, '14': 124902244, '15': 104043685, '16': 98207768, '17': 94987271, '18': 90702639, '19': 61431566, '2': 182113224, '3': 160039680, '4': 156508116, '5': 151834684, '6': 149736546, '7': 145441459, '8': 129401213, '9': 124595110, 'M': 16299, 'X': 171031299, 'Y': 91744698}}

# assumes 'location' is a string formatted like chr8:10-30 or 3:5903-6567 or hpv16ref_1:1-2342
def get_chrom_num(location: str):
    location = location.replace("'", "") # clean any apostrophes out
    raw_contig_id = location.rsplit(":")[0]
    if raw_contig_id.startswith('chr'):
        return raw_contig_id[3:]

    return raw_contig_id


# def get_chrom_lens(ref):
#     chrom_len_dict = {}
#
#     with open(f'bed_files/{ref}_noAlt.fa.fai') as infile:
#         for line in infile:
#             fields = line.rstrip().rsplit()
#             if fields:
#                 if fields[0].startswith('chr'):
#                     chrom_len_dict[fields[0].lstrip('chr')] = int(fields[1])
#                 else:
#                     chrom_len_dict[fields[0]] = int(fields[1])
#
#     return chrom_len_dict


def plot(db_handle, sample, sample_name, project_name, filter_plots=False):
    # SET UP HANDLE
    # collection_handle = get_collection_handle(db_handle, 'projects')
    fs_handle = gridfs.GridFS(db_handle)

    start_time = time.time()
    potential_ref_genomes = set()
    for item in sample:
        ## look for what reference genome is used
        ref_version = item['Reference_version']
        potential_ref_genomes.add(ref_version)

    if len(potential_ref_genomes) > 1:
        logging.warning("\nMultiple reference genomes found in project samples, but each project only supports one "
              "reference genome across samples. Only the first will be used.\n")

    ref = potential_ref_genomes.pop()
    full_cent_dict = ref_cent_dict[ref]

    # deprecated - contents of centromere files moved to variable
    # cent_file = f'bed_files/{ref}_centromere.bed'

    # full_cent_dict = {}
    # with open(cent_file) as infile:
    #     for line in infile:
    #         fields = line.rsplit("\t")
    #         chr_num = get_chrom_num(fields[0])
    #         s, e = int(fields[1]), int(fields[2])
    #         if chr_num not in full_cent_dict:
    #             full_cent_dict[chr_num] = (s, e)
    #         else:
    #             cp = full_cent_dict[chr_num]
    #             full_cent_dict[chr_num] = (min(cp[0], s), max(cp[1], e))


    # updated_loc_dict = defaultdict(list)  # stores the locations following the plotting adjustments
    # deprecated - contents of centromere files moved to variable
    # chrom_lens = get_chrom_lens(ref)
    chrom_lens = ref_length_dict[ref]

    cnv_file_id = sample[0]['CNV_BED_file']
    logging.debug('cnv_file_id: ' + str(cnv_file_id))

    try:
        cnv_file = fs_handle.get(ObjectId(cnv_file_id)).read()
        cnv_decode = str(cnv_file, 'utf-8')
        cnv_string = StringIO(cnv_decode)
        df = pd.read_csv(cnv_string, sep="\t", header=None)
        df.rename(columns={0: 'Chromosome Number', 1: "Feature Start Position", 2: "Feature End Position", 3: 'Source',
                           4: 'Copy Number'}, inplace=True)

    except Exception as e:
        logging.exception(e)
        df = pd.DataFrame(columns=["Chromosome Number", "Feature Start Position", "Feature End Position", "Source",
                                   "Copy Number"])

    # Note, that a 4 column CNV file, instead of a 5 column CNV file may be given. We instruct users to place Copy Number in the last column.

    amplicon = pd.DataFrame(sample)

    amplicon_numbers = sorted(list(amplicon['AA_amplicon_number'].unique()))
    seen = set()

    chr_order = lambda x: int(x) if x.isnumeric() else ord(x[0])
    if filter_plots:
        chromosomes = set()
        for x in amplicon['Location']:
            for loc in x:
                chr_num = get_chrom_num(loc)
                if chr_num:
                    chromosomes.add(chr_num)

        if chromosomes:
            chromosomes = sorted(list(chromosomes), key=chr_order)

        else:
            chromosomes = []

    else:
        if ref == "mm10":
            chromosomes = (
            "1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12", "13", "14", "15", "16", "17", "18", "19",
            "X", "Y")
        else:
            chromosomes = (
            "1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12", "13", "14", "15", "16", "17", "18", "19",
            "20", "21", "22", "X", "Y")


    n_amps = len(amplicon_numbers)
    cmap = cm.get_cmap('Spectral', n_amps + 2)
    amplicon_colors = [f"rgba({', '.join([str(val) for val in cmap(i)])})" for i in range(1, n_amps + 1)]
    #print(df[df['Chromosome Number'] == 'hpv16ref_1'])
    if chromosomes:
        rows = (len(chromosomes) // 4) + 1 if len(chromosomes) % 4 else len(chromosomes) // 4
        fig = make_subplots(rows=rows, cols=4,
            subplot_titles=chromosomes, horizontal_spacing=0.05, vertical_spacing = 0.1 if rows < 4 else 0.05)

        dfs = {}
        for chromosome in df['Chromosome Number'].unique():
            key = get_chrom_num(chromosome)
            value = df[df['Chromosome Number'] == chromosome]
            dfs[key] = value

        ## CREATE ARRAY
        rowind = 1
        colind = 1
        min_width = 0.03  # minimum width before adding padding to make visible on default zoom
        max_width = 0.06  # maximum width before chunking to create hover points inside feature block
        # for key in (chromosomes if filter_plots else dfs):
        for key in chromosomes:
            x_range = chrom_lens[key]
            log_scale = False
            x_array = []
            y_array = []
            if key in dfs:
                if len(dfs[key].columns) >= 4 and is_numeric_dtype(df['Copy Number'][0]):
                    for ind, row in dfs[key].iterrows():
                        # CN Start
                        x_array.append(row[1])
                        y_array.append(float(row[-1]))

                        # CN End
                        if row[2] - row[1] > 10000000:
                            divisor = (row[2] - row[1]) / 10
                            for j in range(1, 11):
                                x_array.append(row[1] + divisor * j)
                                y_array.append(row[-1])

                        else:
                            x_array.append(row[2])
                            y_array.append(row[-1])

                        # Drop off
                        x_array.append(row[2])
                        y_array.append(np.nan)

                if x_array and y_array:
                    x_array = [round(item, 2) for item in x_array]
                    y_array = [round(item, 2) for item in y_array]

            amplicon_df = pd.DataFrame()
            for ind, row in amplicon.iterrows():
                locs = row["Location"]
                for element in locs:
                    chrsplit = element.split(':')
                    chrom = get_chrom_num(element)
                    if chrom == key:
                        curr_updated_loc = chrom + ":"
                        for j in range(0, 2):
                            row['Chromosome Number'] = chrom
                            locsplit = chrsplit[1].split('-')
                            row['Feature Start Position'] = int(float(locsplit[0]))
                            row['Feature End Position'] = int(float(locsplit[1].strip()))

                            relative_width = (int(float(locsplit[1])) - int(float(locsplit[0]))) / x_range
                            if relative_width < min_width:
                                offset = (x_range * min_width) - (int(float(locsplit[1])) - int(float(locsplit[0])))

                            else:
                                offset = 0
                            
                            if j == 0:
                                row['Feature Position'] = int(float(locsplit[0])) - offset//2
                                row['Y-axis'] = 95
                                curr_updated_loc += str(locsplit[0]) + "-"
                                amplicon_df = amplicon_df.append(row)

                            else:
                                if relative_width > max_width:
                                    num_chunks = int(relative_width // max_width)
                                    abs_step = max_width * x_range
                                    spos = int(float(locsplit[0])) - offset//2
                                    for k in range(1, num_chunks):
                                        row['Feature Position'] = spos + k * abs_step
                                        row['Y-axis'] = 95
                                        curr_updated_loc += str(int(row['Feature Position']))
                                        amplicon_df = amplicon_df.append(row)

                                row['Feature Position'] = int(float(locsplit[1])) + offset//2
                                row['Y-axis'] = 95
                                curr_updated_loc += str(int(row['Feature Position']))
                                amplicon_df = amplicon_df.append(row)

                            
                        amplicon_df['Feature Maximum Copy Number'] = amplicon_df['Feature_maximum_copy_number']
                        amplicon_df['Feature Median Copy Number'] = amplicon_df['Feature_median_copy_number']
                        for i in range(len(amplicon_df['AA_amplicon_number'].unique())):
                            number = amplicon_df['AA_amplicon_number'].unique()[i]
                            per_amplicon = amplicon_df[amplicon_df['AA_amplicon_number'] == number]

                            show_legend = number not in seen
                            seen.add(number)

                            amplicon_df2 = amplicon_df[['Classification','Chromosome Number', 'Feature Start Position',
                                                        'Feature End Position','Oncogenes','Feature Maximum Copy Number',
                                                        'AA_amplicon_number', 'Feature Position','Y-axis']]
                            #print(amplicon_df2)
                            oncogenetext = '<i>Oncogenes:</i> %{customdata[4]}<br>' if amplicon_df2['Oncogenes'].iloc[0][0] else ""
                            ht = '<br><i>Feature Classification:</i> %{customdata[0]}<br>' + \
                                 '<i>%{customdata[1]}:</i> %{customdata[2]} - %{customdata[3]}<br>' + \
                                 oncogenetext + \
                                 '<i>Feature Maximum Copy Number:</i> %{customdata[5]}<br>'

                            fig.add_trace(go.Scatter(x = per_amplicon['Feature Position'], y = per_amplicon['Y-axis'],
                                    customdata = amplicon_df2, mode='lines',fill='tozeroy', hoveron='points+fills', hovertemplate=ht,
                                    name = '<b>Amplicon ' + str(number) + '</b>', fillcolor = amplicon_colors[amplicon_numbers.index(number)],
                                    line = dict(color = amplicon_colors[amplicon_numbers.index(number)]),
                                        showlegend=show_legend, legendrank=number, legendgroup='<b>Amplicon ' + str(number) + '</b>'),
                                          row = rowind, col = colind)

                        amplicon_df = pd.DataFrame()

            if key in full_cent_dict:
                cp = full_cent_dict[key]
                clen = cp[1] - cp[0]
                if clen / x_range < min_width:
                    offset = (x_range * min_width) - clen
                else:
                    offset = 0

                cen_data = [[key, cp[0] - offset/2, 95, "-".join([str(x) for x in cp])], [key, cp[1] + offset/2, 95, "-".join([str(x) for x in cp])]]
                chr_df = pd.DataFrame(data=cen_data, columns=['ID', 'Centromere Position', 'Y-axis', 'pos-pair'])

                if rowind == 1 and colind == 1:
                    fig.add_trace(go.Scatter(x = chr_df['Centromere Position'], y = chr_df['Y-axis'], fill = 'tozeroy', mode = 'lines', fillcolor = 'rgba(2, 6, 54, 0.3)',
                        line_color = 'rgba(2, 6, 54, 0.2)', customdata = chr_df, hovertemplate =
                        '<br>%{customdata[0]}: %{customdata[3]}', name = 'Centromere', legendrank=0, legendgroup='Centromere'), row = rowind, col = colind)
                else:
                    fig.add_trace(go.Scatter(x = chr_df['Centromere Position'], y = chr_df['Y-axis'], fill = 'tozeroy', mode = 'lines', fillcolor = 'rgba(2, 6, 54, 0.3)',
                        line_color = 'rgba(2, 6, 54, 0.2)', customdata = chr_df, name = 'Centromere', legendrank=0, showlegend = False, legendgroup='Centromere', hovertemplate =
                        '<br>%{customdata[0]}: %{customdata[3]}'), row = rowind, col = colind)

            fig.add_trace(go.Scatter(x=x_array,y=y_array,mode = 'lines', name="CN", showlegend = (rowind == 1 and colind == 1),
                                     legendrank=0, legendgroup='CN', line = dict(color = 'black')), row = rowind, col = colind)

            fig.update_xaxes(row=rowind, col=colind, range=[0, x_range])

            #print(y_array)
            if any([element > 20 for element in y_array]):
                log_scale = True

            if log_scale:
                fig.update_yaxes(autorange = False, type="log", ticks = 'outside', ticktext = ['0','1', '', '', '', '', '', '', '', '', '10', '', '', '', '', '', '', '', '', '100'],
                    ticklen = 10, showline = True, linewidth = 1, showgrid = False, range = [-0.3, 2], tick0 = 0, dtick = 1, tickmode = 'array',
                    tickvals = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100],
                    ticksuffix = " ", row = rowind, col = colind)
            else:
                fig.update_yaxes(autorange = False, ticks = 'outside', ticklen = 10, range = [0, 20],
                                 ticktext = ['0', '', '10', '', '20'], tickvals = [0, 5, 10, 15, 20],
                                 showline = True, linewidth = 1, showgrid = False,
                                 tick0 = 0, dtick = 1, tickmode = 'array', ticksuffix = " ", row = rowind, col = colind)

            if colind == 1:
                fig.update_yaxes(title = 'Copy Number', row = rowind, col = colind)

            if colind == 4:
                rowind += 1
                colind = 1
            else:
                colind += 1

        height = {
            1: 300,
            2: 520,
            3: 700,
            4: 750,
            5: 900,
            6: 1000
        }

        fig.update_xaxes(showline=True, linewidth=1, title_font_size=10, ticksuffix=" ")
        fig.update_traces(textposition="bottom right")

        # note: setting hoverdistance (measured in pixels) too high will cause spillover of hover text to bad places
        fig.update_layout(title_font_size=30, xaxis = dict(gridcolor='white'), template = None, hovermode = 'x unified',
                          title_text=f"{sample_name} Copy Number Plots", height = height[rows], hoverdistance=2,
                          margin = dict(t = 70, r = 35, b = 15, l = 70))

        # add select and deselect all buttons
        fig.update_layout(dict(updatemenus=[
            dict(
                type="buttons",
                direction="left",
                buttons=list([
                    dict(
                        args=["visible", "legendonly"],
                        label="Deselect All",
                        method="restyle"
                    ),
                    dict(
                        args=["visible", True],
                        label="Select All",
                        method="restyle"
                    )
                ]),
                pad={"r": 0, "t": 10},
                showactive=False,
                x=1.15,
                xanchor="right",
                y=1.1,
                yanchor="bottom"
            ),
        ]
        ))


        end_time = time.time()
        elapsed_time = end_time - start_time
        logging.info(f"Created sample plot in {elapsed_time} seconds")

        updated_config_dict = {'toImageButtonOptions': {
                                   'format': 'svg',  # one of png, svg, jpeg, webp
                                }
                               }

        return fig.to_html(full_html=False, config=updated_config_dict, div_id='plotly_div')

    else:
        plot = go.Figure(go.Scatter(x=[2], y = [2],
                                     mode="markers+text",
                                       text=['No Amplicons Detected'],
                                       textposition='middle center',
                                       textfont = dict(
                                            family = 'sans serif',
                                            size = 50,
                                            color = "crimson"
                                       ))).to_html(full_html=False, div_id='plotly_div')

        return plot
