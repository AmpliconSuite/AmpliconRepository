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
                if fields[0].startswith('chr'):
                    chrom_len_dict[fields[0].lstrip('chr')] = int(fields[1])
                else:
                    chrom_len_dict[fields[0]] = int(fields[1])

    return chrom_len_dict


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
    cent_file = f'bed_files/{ref}_centromere.bed'

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


    # updated_loc_dict = defaultdict(list)  # stores the locations following the plotting adjustments
    chrom_lens = get_chrom_lens(ref)

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
