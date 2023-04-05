from statistics import mode
from dash import Dash, html, dcc
import plotly.express as px
#import dash_bio as dashbio
import pandas as pd
import plotly.graph_objs as go
import numpy as np
from numpy import random
import warnings
from plotly.subplots import make_subplots
from pylab import cm
import matplotlib.pyplot as plt
from matplotlib.colors import ListedColormap
import os
import re
from .utils import get_db_handle, get_collection_handle, create_run_display
import gridfs
from bson.objectid import ObjectId
from io import StringIO
from collections import defaultdict
import time

warnings.filterwarnings("ignore")

# FOR LOCAL DEVELOPMENT
# db_handle, mongo_client = get_db_handle('caper', 'mongodb://localhost:27017')

# FOR PRODUICTION
db_handle, mongo_client = get_db_handle('caper', os.environ['DB_URI'])

# SET UP HANDLE
collection_handle = get_collection_handle(db_handle,'projects')
fs_handle = gridfs.GridFS(db_handle)


# assumes 'location' is a string formatted like chr8:10-30 or 3:5903-6567 or hpv16ref_1:1-2342
def get_chrom_num(location: str):
    location = location.replace("'", "") # clean any apostrophes out
    raw_contig_id = location.rsplit(":")[0]
    if raw_contig_id.startswith('chr'):
        return raw_contig_id[3:]

    return raw_contig_id


def get_chrom_lens(ref):
    chrom_len_dict = {}
    with open(f'bed_files/{ref}_noAlt.fa.fai') as infile:
        for line in infile:
            fields = line.rstrip().rsplit()
            if fields:
                chrom_len_dict[fields[0].lstrip('chr')] = int(fields[1])

    return chrom_len_dict


def plot(sample, sample_name, project_name, filter_plots=False):
    # project_data_dir = f'project_data/{project_name}/extracted'
    # if not os.path.exists(project_data_dir):
    #     return ''
    start_time = time.time()
    potential_ref_genomes = set()
    for item in sample:
        ## look for what reference genome is used
        ref_version = item['Reference_version']
        potential_ref_genomes.add(ref_version)

    if len(potential_ref_genomes) > 1:
        print("\nWARNING! Multiple reference genomes found in project samples, but each project only supports one "
              "reference genome across samples.\n")

    ref = potential_ref_genomes.pop()
    cent_file = f'bed_files/{ref}_centromere.bed'
    full_cent_df = pd.read_csv(cent_file, header=None, sep='\t')
    for i, row in full_cent_df.iterrows():
        chr_num = get_chrom_num(row[0])
        full_cent_df.at[i, 0] = chr_num

    # updated_loc_dict = defaultdict(list)  # stores the locations following the plotting adjustments
    chrom_lens = get_chrom_lens(ref)
    cnv_file_id = sample[0]['CNV_BED_file']

    try:
        cnv_file = fs_handle.get(ObjectId(cnv_file_id)).read()
        cnv_decode = str(cnv_file, 'utf-8')
        cnv_string = StringIO(cnv_decode)
        df = pd.read_csv(cnv_string, sep="\t", header=None)
        df.rename(columns={0: 'Chromosome Number', 1: "Feature Start Position", 2: "Feature End Position", 3: 'Source',
                           4: 'Copy Number'}, inplace=True)

    except Exception as e:
        print(e)
        df = pd.DataFrame(columns=["Chromosome Number", "Feature Start Position", "Feature End Position", "Source",
                                   "Copy Number"])

    # Note, that a 4 column CNV file, instead of a 5 column CNV file may be given. We instruct users to place Copy Number in the last column.


    amplicon = pd.DataFrame(sample)
    # amplicon['AA amplicon number'] = amplicon['AA amplicon number'].astype(int).astype(str)

    amplicon_numbers = sorted(list(amplicon['AA_amplicon_number'].unique()))
    seen = set()

    chr_order = lambda x: int(x) if x.isnumeric() else ord(x[0])
    if filter_plots:
        chromosomes = set()
        for x in amplicon['Location']:
            # if len(x) > 1:
            for loc in x:
                chr_num = get_chrom_num(loc)
                if chr_num:
                    chromosomes.add(chr_num)
            # else:
            #     chr_num = get_chrom_num(x[0])
            #     chromosomes.add(chr_num)
        if chromosomes:
            chromosomes = sorted(list(chromosomes), key=chr_order)

        else:
            chromosomes = []

    else:
        chromosomes = ("1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12", "13", "14", "15", "16", "17", "18", "19", "20", "21", "22", "X", "Y")
    
    cmap = cm.get_cmap('Spectral', len(amplicon['AA_amplicon_number'].unique()))
    amplicon_colors = [f"rgba({', '.join([str(val) for val in cmap(i)])})" for i in range(cmap.N)]

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
        min_width = 0.03
        # for key in (chromosomes if filter_plots else dfs):
        for key in chromosomes:
            x_range = chrom_lens[key]
            log_scale = False
            x_array = []
            y_array = []
            if key in dfs:
                if len(dfs[key].columns) >= 4:
                    for ind, row in dfs[key].iterrows():
                        # CN Start
                        x_array.append(row[1])
                        y_array.append(row[-1])

                        if row[2] - row[1] > 10000000:
                            divisor = (row[2] - row[1]) / 10
                            for j in range(1, 11):
                                x_array.append(row[1] + divisor * j)
                                y_array.append(row[-1])

                        else:
                            # CN End
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

                            if (int(float(locsplit[1])) - int(float(locsplit[0]))) / x_range < min_width:
                                offset = (x_range * min_width) - (int(float(locsplit[1])) - int(float(locsplit[0])))
                            else:
                                offset = 0

                            if j == 0:
                                row['Feature Position'] = locsplit[0]
                                row['Y-axis'] = 95
                                curr_updated_loc += str(locsplit[0]) + "-"
                            elif j == 1:
                                row['Feature Position'] = int(float(locsplit[1])) + offset
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
                            # amplicon_df2 = amplicon_df2.astype({'AA PNG file':'string'})
                            # print(amplicon_df.head())
                            fig.add_trace(go.Scatter(x = per_amplicon['Feature Position'], y = per_amplicon['Y-axis'],
                                    customdata = amplicon_df2, mode='lines',fill='tozeroy', hoveron='points+fills', hovertemplate=
                                    '<br><i>Feature Classification:</i> %{customdata[0]}<br>' +
                                    '<i>%{customdata[1]}:</i> %{customdata[2]} - %{customdata[3]}<br>' +
                                    '<i>Oncogenes:</i> %{customdata[4]}<br>'+
                                    '<i>Feature Maximum Copy Number:</i> %{customdata[5]}<br>' +
                                    '<b>Click to Download Amplicon PNG</b>'
                                    ,name = '<b>Amplicon ' + str(number) + '</b>', opacity = 0.3, fillcolor = amplicon_colors[amplicon_numbers.index(number)],
                                    line = dict(color = amplicon_colors[amplicon_numbers.index(number)]),
                                        showlegend=show_legend, legendrank=number),
                                          row = rowind, col = colind)

                        amplicon_df = pd.DataFrame()

            #display(a_df)
            cent_df = full_cent_df[full_cent_df[0] == key]
            #display(cent_df)
            chr_df = pd.DataFrame()
            for i in range(len(cent_df)):
                row = cent_df.iloc[[i]]
                if (row.iloc[0, 2] - row.iloc[0, 1]) / x_range < min_width:
                    offset = (x_range * min_width) - (row.iloc[0, 2] - row.iloc[0, 1])
                    # offset = 0
                else:
                    offset = 0

                for j in range(0, 2):
                    if j == 0:
                        row['Centromere Position'] = row.iloc[0, 1]
                        row['Y-axis'] = 95
                    elif j == 1:
                        row['Centromere Position'] = row.iloc[0, 2] + offset
                        row['Y-axis'] = 95
                    chr_df = pd.concat([row, chr_df])



            if rowind == 1 and colind == 1:
                fig.add_trace(go.Scatter(x = chr_df['Centromere Position'], y = chr_df['Y-axis'], fill = 'tozeroy', mode = 'lines', fillcolor = 'rgba(2, 6, 54, 0.3)',
                    line_color = 'rgba(2, 6, 54, 0.2)', customdata = chr_df, hovertemplate =
                    '<br>%{customdata[0]}: %{customdata[1]}-%{customdata[2]}', name = 'Centromere', legendrank=0), row = rowind, col = colind)
            else:
                fig.add_trace(go.Scatter(x = chr_df['Centromere Position'], y = chr_df['Y-axis'], fill = 'tozeroy', mode = 'lines', fillcolor = 'rgba(2, 6, 54, 0.3)',
                    line_color = 'rgba(2, 6, 54, 0.2)', customdata = chr_df, name = 'Centromere', showlegend = False, hovertemplate =
                    '<br>%{customdata[0]}: %{customdata[1]}-%{customdata[2]}'
                    ), row = rowind, col = colind)

            fig.add_trace(go.Scatter(x=x_array,y=y_array,mode = 'lines', name="Copy Number", showlegend = False, line = dict(color = 'black')), row = rowind, col = colind)
            fig.update_xaxes(row=rowind, col=colind, range=[0, x_range])

            #print(y_array)
            for element in y_array:
                if element > 20:
                    log_scale = True

            if log_scale:
                fig.update_yaxes(autorange = False, type="log", ticks = 'outside', ticktext = ['0','1', '', '', '', '', '', '', '', '', '10', '100'],
                    ticklen = 10, showline = True, linewidth = 1, showgrid = False, range = [0,2], tick0 = 0, dtick = 1, tickmode = 'array', tickvals = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 100],
                    ticksuffix = " ", row = rowind, col = colind)
            else:
                fig.update_yaxes(autorange = False, ticks = 'outside', ticklen = 10, range = [0, 20], ticktext = ['0', '', '10', '', '20'], tickvals = [0, 5, 10, 15, 20], showline = True, linewidth = 1, showgrid = False,
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
        fig.update_layout(title_font_size=30,
        xaxis = dict(gridcolor='white'), template = None, hovermode = 'x unified', title_text=f"{sample_name} Copy Number Plots",
        height = height[rows], width = 1300, margin = dict(t = 70, r = 70, b = 70, l = 70))
        end_time = time.time()
        elapsed_time = end_time - start_time
        print(f"Created a sample plot in {elapsed_time} seconds")
        return fig.to_html(full_html=False, div_id='plotly_div')

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