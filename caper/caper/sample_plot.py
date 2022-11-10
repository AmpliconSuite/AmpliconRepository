from statistics import mode
from dash import Dash, html, dcc
import plotly.express as px
import dash_bio as dashbio
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

cent_file = 'bed_files/GRCh38_centromere.bed'
warnings.filterwarnings("ignore")


def plot(sample, sample_name, project_name, filter_plots=True):
    project_data_dir = f'project_data/{project_name}/extracted'
    if not os.path.exists(project_data_dir):
        return ''

    CNV_file = sample[0]['CNV BED file']
    CNV_file = CNV_file[CNV_file.index('AA_outputs'):]
    CNV_file = f"{project_data_dir}/{CNV_file}"
    amplicon = pd.DataFrame(sample)

    amplicon['Oncogenes'] = amplicon['Oncogenes'].str.replace('[', '')
    amplicon['Oncogenes'] = amplicon['Oncogenes'].str.replace("'", "")
    amplicon['Oncogenes'] = amplicon['Oncogenes'].str.replace(']', '')
    amplicon['Location'] = amplicon['Location'].str.replace('[', '')
    amplicon['Location'] = amplicon['Location'].str.replace("'", "")
    amplicon['Location'] = amplicon['Location'].str.replace(']', '')
    
    # valid_range = lambda loc: int(loc[1]) - int(loc[0]) > 1000000
    # valid_amp = lambda x: any(valid_range(loc.split(':')[1].split('-')) for loc in x['Location'].split(', '))
    # valid_amp_df = lambda df: [valid_amp(row) for row in df.iloc]
    # amplicon = amplicon[valid_amp_df(amplicon)]
    amplicon_numbers = list(amplicon['AA amplicon number'].unique())
    seen = set()
    
    chr_order = lambda x: int(x) if x.isnumeric() else ord(x)
    if filter_plots:
        chromosomes = list(sorted(set(loc.split(':')[0].lstrip('chr') for loc in (', '.join(amplicon['Location'])).split(', ')), key=chr_order))
    else:
        chromosomes = ("1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11",
        "12", "13", "14", "15", "16", "17", "18", "19", "20", "21", "22",
        "X", "Y")
    
    cmap = cm.get_cmap('Spectral', len(amplicon['AA amplicon number'].unique()))
    amplicon_colors = [f"rgba({', '.join([str(val) for val in cmap(i)])})" for i in range(cmap.N)]

    rows = (len(chromosomes) // 4) + 1 if len(chromosomes) % 4 else len(chromosomes) // 4
    fig = make_subplots(rows=rows, cols=4
        ,subplot_titles=chromosomes, horizontal_spacing=0.05, vertical_spacing = 0.1 if rows < 4 else 0.05)

    df = pd.read_csv(CNV_file, sep = "\t", header = None)
    df.rename(columns = {0: 'Chromosome Number', 1: "Feature Start Position", 2: "Feature End Position", 3: 'CNV Gain', 4: 'Copy Number'}, inplace = True)
    dfs = {}
    
    for chromosome in df['Chromosome Number'].unique():
        key = chromosome
        value = df[df['Chromosome Number'] == chromosome]
        dfs[key] = value

    ## CREATE ARRAY

    rowind = 1
    colind = 1

    for key in (chromosomes if filter_plots else dfs):
        if filter_plots:
            key = 'chr' + key
        log_scale = False
        copyNumberdf = pd.DataFrame()
        x_array = []
        y_array = []
        for i in range(len(dfs[key])):
            #CN Start
            x_array.append(dfs[key].iloc[i, 1])
            y_array.append(dfs[key].iloc[i, 4])

            if dfs[key].iloc[i, 2] - dfs[key].iloc[i, 1] > 10000000:
                divisor = (dfs[key].iloc[i, 2] - dfs[key].iloc[i, 1]) / 10
                for j in range(1, 11):
                    x_array.append(dfs[key].iloc[i, 1] + divisor * j)
                    y_array.append(dfs[key].iloc[i, 4])
            else:
                #CN End
                x_array.append(dfs[key].iloc[i, 2])
                y_array.append(dfs[key].iloc[i, 4])
            #Drop off
            x_array.append(dfs[key].iloc[i, 2])
            y_array.append(np.nan)


        x_array = [round(item, 2) for item in x_array]
        y_array = [round(item, 2) for item in y_array]

        x_range = max(x_array) - min(x_array)
        min_width = 0.03

        amplicon_df = pd.DataFrame()
        for i in range(len(amplicon)):
            row = amplicon.iloc[[i]]
            loc = row.iloc[0, 4]
            splitloc = loc.split(',')
            for element in splitloc:
                chrsplit = element.split(':')
                chr = chrsplit[0]
                if chr == key or chr[1:] == key:
                    for j in range(0,2):
                        row['Chromosome Number'] = chrsplit[0]
                        locsplit = chrsplit[1].split('-') 
                        row['Feature Start Position'] = int(locsplit[0])
                        row['Feature End Position'] = int(locsplit[1])

                        if (int(locsplit[1]) - int(locsplit[0])) / x_range < min_width:
                            offset = (x_range * min_width) - (int(locsplit[1]) - int(locsplit[0]))
                        else:
                            offset = 0

                        if j == 0:
                            row['Feature Position'] = locsplit[0]
                            row['Y-axis'] = 95
                        elif j == 1:
                            row['Feature Position'] = int(locsplit[1]) + offset
                            row['Y-axis'] = 95
                        amplicon_df = pd.concat([row, amplicon_df])
                    amplicon_df['Feature Start Position'] = amplicon_df['Feature Start Position'].astype(float)
                    amplicon_df['Feature End Position'] = amplicon_df['Feature End Position'].astype(float)
                    amplicon_df['Feature Position'] = amplicon_df['Feature Position'].astype(float)
                    amplicon_df['Feature Maximum Copy Number'] = amplicon_df['Feature maximum copy number'].astype(float)
                    amplicon_df['Feature Median Copy Number'] = amplicon_df['Feature median copy number'].astype(float)
                    amplicon_df = amplicon_df.round(decimals=2)
                    for i in range(len(amplicon_df['AA amplicon number'].unique())):
                        number = amplicon_df['AA amplicon number'].unique()[i]
                        per_amplicon = amplicon_df[amplicon_df['AA amplicon number'] == number]
                        
                        show_legend = number not in seen
                        seen.add(number)
                        
                        fig.add_trace(go.Scatter(x = per_amplicon['Feature Position'], y = per_amplicon['Y-axis'], 
                                customdata = amplicon_df, mode='lines',fill='tozeroy', hoveron='points+fills', hovertemplate=
                                '<br><i>Feature Classification:</i> %{customdata[3]}<br>' + 
                                '<i>%{customdata[16]}:</i> %{customdata[17]} - %{customdata[18]}<br>' +
                                '<i>Oncogenes:</i> %{customdata[5]}<br>'+
                                '<i>Feature Maximum Copy Number:</i> %{customdata[9]}<br>'
                                f'<b class="/{project_data_dir}/AA_outputs/{sample_name}/{sample_name}_AA_results/{sample_name}_amplicon{number}.png">Click to Download Amplicon {number} PNG</b>',
                                name = '<b>Amplicon ' + str(number) + '</b>', opacity = 0.3, fillcolor = amplicon_colors[amplicon_numbers.index(number)],
                                line = dict(color = amplicon_colors[amplicon_numbers.index(number)]), showlegend=show_legend, legendrank=number, text='hallo'), row = rowind, col = colind)
                        fig.update_traces(textposition="bottom right")

                    amplicon_df = pd.DataFrame()

        cent_df = pd.read_csv(cent_file, header = None, sep = '\t')
        #display(a_df)
        cent_df = cent_df[cent_df[0] == key]
        #display(cent_df)
        chr_df = pd.DataFrame()
        for i in range(len(cent_df)):
            row = cent_df.iloc[[i]]
            if (row.iloc[0, 2] - row.iloc[0, 1]) / x_range < min_width:
                offset = (x_range * min_width) - (row.iloc[0, 2] - row.iloc[0, 1])
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
        fig.update_xaxes(showline = True, linewidth = 1, title_font_size = 10,  ticksuffix = " ")

        #print(y_array)
        for element in y_array:
            if element > 20:
                log_scale = True

        if log_scale:
            fig.update_yaxes(autorange = False, type="log", ticks = 'outside', ticktext = ['0','1', '', '', '', '', '', '', '', '', '10', '100'], 
                ticklen = 10, showline = True, linewidth = 1, showgrid = False, range = [0,2], tick0 = 0, dtick = 1, tickmode = 'array', tickvals = [0,1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 100], 
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

    fig.update_layout(title_font_size=30,  
    xaxis = dict(gridcolor='white'), template = None, hovermode = 'x unified', title_text=f"{sample_name} Copy Number Plots",
    height = height[rows], width = 1300, margin = dict(t = 70, r = 70, b = 70, l = 70))

    return fig.to_html(full_html=False, div_id='plotly_div')

