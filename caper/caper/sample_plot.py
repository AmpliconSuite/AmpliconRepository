from statistics import mode
from dash import Dash, html, dcc
import plotly.express as px
import dash_bio as dashbio
import pandas as pd
import plotly.graph_objs as go
import numpy as np
import warnings
from plotly.subplots import make_subplots


def plot(sample):

    warnings.filterwarnings("ignore")

    fig = make_subplots(
        rows=6, cols=4,
        subplot_titles=("Chromosome 1", "Chromosome 2", "Chromosome 3", "Chromosome 4", "Chromosome 5", "Chromosome 6", "Chromosome 7", "Chromosome 8", "Chromosome 9", "Chromosome 10", "Chromosome 11",
        "Chromosome 12", "Chromosome 13", "Chromosome 14", "Chromosome 15", "Chromosome 16", "Chromosome 17", "Chromosome 18", "Chromosome 19", "Chromosome 20", "Chromosome 21", "Chromosome 22",
        "Chromosome X", "Chromosome Y"))

    CNV_FILE_ABS = sample["CNV BED file"]
    CNV_FILE = CNV_FILE_ABS[CNV_FILE_ABS.index("AA_outputs"):]

    df = pd.read_csv(CNV_FILE, sep = "\t", header = None)
    df.rename(columns = {0: 'Chromosome Number', 1: "Feature Start Position", 2: "Feature End Position", 3: 'CNV Gain', 4: 'Copy Number'}, inplace = True)
    dfs = {}
    for chromosome in df['Chromosome Number'].unique():
        key = chromosome
        value = df[df['Chromosome Number'] == chromosome]
        dfs[key] = value

    ## CREATE ARRAY

    #display(dfs['chr11'])
    rowind = 1
    colind = 1
    for key in dfs:
        x_array = []
        y_array = []
        # print(key)
        for i in range(len(dfs[key])):
            x_array.append(dfs[key].iloc[i, 1])
            x_array.append(dfs[key].iloc[i, 2])
            y_array.append(dfs[key].iloc[i, 4])
            y_array.append(dfs[key].iloc[i, 4])

            x_array.append(dfs[key].iloc[i, 2])
            y_array.append(np.nan)

        fig.add_trace(go.Scatter(x=x_array,y=y_array,mode = 'lines', name="Copy Number", showlegend = False), row = rowind, col = colind)

        amplicon2 = pd.DataFrame(sample)
        #display(amplicon2)
        amplicon_df = pd.DataFrame()
        for i in range(len(amplicon2)):
            row = amplicon2.iloc[[i]]
            loc = row.iloc[0, 4]
            splitloc = loc.split(',')
            for element in splitloc:
                chrsplit = element.split(':')
                #print(chrsplit)
                chr = chrsplit[0][2:]
                if chr == key:
                    for j in range(0,4):
                        row['Chromosome Number'] = chrsplit[0]
                        locsplit = chrsplit[1].split('-') 
                        row['Amplicon Start Position'] = locsplit[0]
                        row['Amplicon End Position'] = locsplit[1][:-1]
                        if j == 0:
                            row['Amplicon Position'] = locsplit[0]
                            row['Y-axis'] = 95
                        elif j == 1:
                            row['Amplicon Position'] = locsplit[0]
                            row['Y-axis'] = 0
                        elif j == 2:
                            row['Amplicon Position'] = locsplit[1][:-1]
                            row['Y-axis'] = 0
                        elif j == 3:
                            row['Amplicon Position'] = locsplit[1][:-1]
                            row['Y-axis'] = 95
                        amplicon_df = pd.concat([amplicon_df, row])

        #display(amplicon_df)
        if not amplicon_df.empty:
            for i in range(len(amplicon_df['AA amplicon number'].unique())):
                number = amplicon_df['AA amplicon number'].unique()[i]
                per_amplicon = amplicon_df[amplicon_df['AA amplicon number'] == number]
                fig.add_trace(go.Scatter(x = per_amplicon['Amplicon Position'], y = per_amplicon['Y-axis'], 
                    customdata = amplicon_df, mode='lines',fill='tozeroy', hoveron='points', hovertemplate=
                    '<br><i>Amplicon Start Position:</i> %{customdata[17]}<br>' +
                    '<b>Amplicon End Position:</b> %{customdata[18]}<br>' +
                    '<i>Feature Classification:</i> %{customdata[3]}<br>' +
                    '<b>Oncogenes:</b> %{customdata[5]}<br>'+
                    '<b><i>Feature Maximum Copy Number:</i></b> %{customdata[9]}<br>'
                    '<i>Feature Average Copy Number</i> %{customdata[8]}<br>'
                    '<a href = "www.google.com">AA PNG File</a>',
                    name = '<b>Amplicon ' + str(number) + '</b>', opacity = 0.3), row = rowind, col = colind)

        a_df = pd.read_csv('bed_files/GRCh38_centromere.bed', header = None, sep = '\t')
        a_df[a_df[0] == key]
        cent_pos_list = []
        cent_pos_list.append(a_df.iloc[0, 1])
        cent_pos_list.append(a_df.iloc[0, 2])
        cent_pos_list.append(a_df.iloc[1, 1])
        cent_pos_list.append(a_df.iloc[1, 2])

        Y_axis = []
        Y_axis.append(95)
        Y_axis.append(95)
        Y_axis.append(95)
        Y_axis.append(95)

        if rowind == 1 and colind == 1:
            fig.add_trace(go.Scatter(x = cent_pos_list, y = Y_axis, fill = 'tozeroy', mode = 'lines', fillcolor = 'rgba(2, 6, 54, 0.3)', line_color = 'rgba(2, 6, 54, 0.3)', name = 'Centromere'), row = rowind, col = colind)
        else:
            fig.add_trace(go.Scatter(x = cent_pos_list, y = Y_axis, fill = 'tozeroy', mode = 'lines', fillcolor = 'rgba(2, 6, 54, 0.3)', line_color = 'rgba(2, 6, 54, 0.3)', name = 'Centromere', showlegend = False), row = rowind, col = colind)

        fig.update_xaxes(showline = True, linewidth = 1, title = 'Genomic Position', title_font_size = 20)
        fig.update_yaxes(autorange = False, type="log", ticks = 'outside', ticktext = ['0','1', '', '', '', '', '', '', '', '', '10', '100'], ticklen = 10, showline = True, linewidth = 1, showgrid = False, range = [0,2], tick0 = 0, dtick = 1, tickmode = 'array', tickvals = [0,1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 100], title = 'Copy Number', title_font_size = 20)

        if colind == 4:
            rowind += 1
            colind = 1
        else:
            colind += 1

    fig.update_layout(title_font_size=30,  xaxis = dict(showgrid = False), template = None, hovermode = 'x unified', height = 1100)

    return [fig.to_html(full_html=False, default_height=500, default_width=800)]
