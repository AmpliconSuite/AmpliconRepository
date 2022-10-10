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
import os
import re

cent_file = 'bed_files/GRCh38_centromere.bed'
warnings.filterwarnings("ignore")

def plot(sample, sample_name, project_name):
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

    amplicon_colors = []
    for num in amplicon['AA amplicon number'].unique():
        r = random.randint(255)
        g = random.randint(255)
        b = random.randint(255)
        amplicon_colors.append('rgba(' + str(r) + ',' + str(g) + ',' + str(b) + ',' + '0.3)')

    fig = make_subplots(rows=6, cols=4
        ,subplot_titles=("1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11",
        "12", "13", "14", "15", "16", "17", "18", "19", "20", "21", "22",
        "X", "Y"), horizontal_spacing=0.05, vertical_spacing = 0.07)

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

    amplicon_numbers = []
    for key in dfs:
        log_scale = False
        copyNumberdf = pd.DataFrame()
        x_array = []
        y_array = []
        for i in range(len(dfs[key])):
            x_array.append(dfs[key].iloc[i, 1])
            x_array.append(dfs[key].iloc[i, 2])
            y_array.append(dfs[key].iloc[i, 4])
            y_array.append(dfs[key].iloc[i, 4])

            x_array.append(dfs[key].iloc[i, 2])
            y_array.append(np.nan)

        x_array = [round(item, 2) for item in x_array]
        y_array = [round(item, 2) for item in y_array]
        fig.add_trace(go.Scatter(x=x_array,y=y_array,mode = 'lines', name="Copy Number", showlegend = False, line = dict(color = 'green')), row = rowind, col = colind)

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
                        row['Feature Start Position'] = locsplit[0]
                        row['Feature End Position'] = locsplit[1]
                        if j == 0:
                            row['Feature Position'] = locsplit[0]
                            row['Y-axis'] = 95
                        elif j == 1:
                            row['Feature Position'] = locsplit[1]
                            row['Y-axis'] = 95
                        amplicon_df = pd.concat([row, amplicon_df])
        #display(amplicon_df)
        
        if not amplicon_df.empty:
            #display(amplicon_df)
            amplicon_df['Feature Start Position'] = amplicon_df['Feature Start Position'].astype(float)
            amplicon_df['Feature End Position'] = amplicon_df['Feature End Position'].astype(float)
            amplicon_df['Feature Position'] = amplicon_df['Feature Position'].astype(float)
            amplicon_df['Feature Maximum Copy Number'] = amplicon_df['Feature maximum copy number'].astype(float)
            amplicon_df['Feature Median Copy Number'] = amplicon_df['Feature median copy number'].astype(float)
            amplicon_df = amplicon_df.round(decimals=2)

            for i in range(len(amplicon_df['AA amplicon number'].unique())):
                number = amplicon_df['AA amplicon number'].unique()[i]
                per_amplicon = amplicon_df[amplicon_df['AA amplicon number'] == number]
                if number not in amplicon_numbers:
                    fig.add_trace(go.Scatter(x = per_amplicon['Feature Position'], y = per_amplicon['Y-axis'], 
                        customdata = amplicon_df, mode='lines',fill='tozeroy', hoveron='points+fills', hovertemplate=
                        '<br><i>Feature Classification:</i> %{customdata[3]}<br>' + 
                        '<i>%{customdata[16]}:</i> %{customdata[17]} - %{customdata[18]}<br>' +
                        '<i>Oncogenes:</i> %{customdata[5]}<br>'+
                        '<i>Feature Maximum Copy Number:</i> %{customdata[9]}<br>'
                        f'<b class="/{project_data_dir}/AA_outputs/{sample_name}/{sample_name}_AA_results/{sample_name}_amplicon{number}.png">Click to Download Amplicon {number} PNG</b>',
                        name = '<b>Amplicon ' + str(number) + '</b>', opacity = 0.3, fillcolor = amplicon_colors[number - 1], 
                        line = dict(color = amplicon_colors[number - 1])), row = rowind, col = colind)
                    amplicon_numbers.append(number)
                else:
                    fig.add_trace(go.Scatter(x = per_amplicon['Feature Position'], y = per_amplicon['Y-axis'], 
                        customdata = amplicon_df, mode='lines',fill='tozeroy', hoveron='points+fills', hovertemplate=
                        '<br><i>Feature Classification:</i> %{customdata[3]}<br>' + 
                        '<i>%{customdata[16]}:</i> %{customdata[17]} - %{customdata[18]}<br>' +
                        '<i>Oncogenes:</i> %{customdata[5]}<br>'+
                        '<i>Feature Maximum Copy Number:</i> %{customdata[9]}<br>'
                        '<b href = "www.google.com">AA PNG File</a>',
                        name = '<b>Amplicon ' + str(number) + '</b>', opacity = 0.3, fillcolor = amplicon_colors[number - 1], 
                        line = dict(color = amplicon_colors[number - 1]), showlegend = False), row = rowind, col = colind)

        cent_df = pd.read_csv(cent_file, header = None, sep = '\t')
        #display(a_df)
        cent_df = cent_df[cent_df[0] == key]
        #display(cent_df)
        chr_df = pd.DataFrame()
        for i in range(len(cent_df)):
            row = cent_df.iloc[[i]]
            #display(row)
            for j in range(0, 2):
                if j == 0:
                    row['Centromere Position'] = row.iloc[0, 1]
                    row['Y-axis'] = 95
                elif j == 1:
                    row['Centromere Position'] = row.iloc[0, 2]
                    row['Y-axis'] = 95
                chr_df = pd.concat([row, chr_df])

        if rowind == 1 and colind == 1:
            fig.add_trace(go.Scatter(x = chr_df['Centromere Position'], y = chr_df['Y-axis'], fill = 'tozeroy', mode = 'lines', fillcolor = 'rgba(2, 6, 54, 0.3)', 
                line_color = 'rgba(2, 6, 54, 0.3)', customdata = chr_df, hovertemplate = 
                '<br>%{customdata[0]}: %{customdata[1]}-%{customdata[2]}', name = 'Centromere'), row = rowind, col = colind)
        else:
            fig.add_trace(go.Scatter(x = chr_df['Centromere Position'], y = chr_df['Y-axis'], fill = 'tozeroy', mode = 'lines', fillcolor = 'rgba(2, 6, 54, 0.3)', 
                line_color = 'rgba(2, 6, 54, 0.3)', customdata = chr_df, name = 'Centromere', showlegend = False, hovertemplate = 
                '<br>%{customdata[0]}: %{customdata[1]}-%{customdata[2]}'
                ), row = rowind, col = colind)

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
            fig.update_yaxes(autorange = False, ticks = 'outside', ticklen = 10, range = [0, 20], ticktext = ['0', '', '', '', '20'], tickvals = [0, 5, 10, 15, 20], showline = True, linewidth = 1, showgrid = False, 
                tick0 = 0, dtick = 1, tickmode = 'array', ticksuffix = " ", row = rowind, col = colind)

        if colind == 1:
            fig.update_yaxes(title = 'Copy Number', row = rowind, col = colind)

        if colind == 4:
            rowind += 1
            colind = 1
        else:
            colind += 1

    fig.update_layout(title_font_size=30,  
    xaxis = dict(gridcolor='white'), template = None, hovermode = 'x unified', title_text=f"{sample_name} Copy Number Plots",
    height = 1000, width = 1300, margin = dict(t = 70, r = 70, b = 70, l = 70))
    
    plot_div = fig.to_html(full_html=False, default_height=500, default_width=800)

    # https://community.plotly.com/t/hyperlink-to-markers-on-map/17858/6
    # Get id of html div element that looks like
    # <div id="301d22ab-bfba-4621-8f5d-dc4fd855bb33" ... >
    res = re.search('<div id="([^"]*)"', plot_div)
    div_id = res.groups()[0]
    # Build JavaScript callback for handling clicks
    # and opening the URL in the trace's customdata 
    js_callback = """
    <script>
    var plot_element = document.getElementById("{div_id}");
    plot_element.on('plotly_click', function(data){{
        var link = '';
        for (let i = 0; i < data['points'].length; i++) {{
            var name = data['points'][i]['data']['name'];
            if (name.includes('Amplicon')) {{
                link = data['points'][i]['data']['hovertemplate'].split('"')[1];
                break;
            }}
        }}

        if (link != '') {{
            var link_window = document.getElementById("figure_download_window");
            var link_elem = link_window.firstChild;
            link_elem.href = link;
            console.log(link_elem);
            link_elem.innerHTML = '<b>Download ' + name.slice(3, -4) + ' PNG </b>';
            link_window.setAttribute('style', 'display: flex; align-items: center;');
        }}
    }})

    var closebtn = document.getElementById("close");
    closebtn.addEventListener("click", function() {{
        this.parentElement.style.display = 'none';
    }});
    </script>
    """.format(div_id=div_id)

    # Build HTML string
    html_str = """
    <div id="figure_download_window", style='display: none'><a href='' download="download" style='border-style: solid; padding: 0.1rem 0.5rem; border-width: 0.15rem'></a><span id='close' style='margin-left: 5px; color: grey'>&times</span></div>
    {plot_div}
    {js_callback}
    """.format(plot_div=plot_div, js_callback=js_callback)

    return html_str
